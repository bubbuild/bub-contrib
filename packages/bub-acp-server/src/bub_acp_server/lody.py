"""Lody-specific behavior of the ACP server, kept out of the core agent.

Everything here is an ACP extension that only Lody understands: the
``agentCapabilities._meta.lody`` block, automatic session titles, the subagent
event stream (``_lody/subagents/event``) and the subagent task requests
(``_lody/subagents/list|cancel|output``). ``BubACPAgent`` drives it through the
generic ``ACPExtension`` hooks and knows nothing about Lody itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp.exceptions import RequestError
from acp.schema import (
    ClientCapabilities,
    Implementation,
    SessionInfoUpdate,
    ToolCallProgress,
)
from bub.streaming import StreamEvent

from bub_acp_server.agent import (
    ACPPromptBlock,
    ACPSession,
    BubACPAgent,
    _block_type,
    _block_value,
    _bub_session_id,
    _event_usage,
    _list_payload,
    _stringify,
    _tool_call_id,
    _tool_kind,
    _tool_name,
    _tool_raw_input,
    _tool_result_outcome,
    _usage_total_tokens,
)
from bub_acp_server.subagents import (
    BUB_SUBAGENT_TASKS,
    LODY_SUBAGENT_EVENTS_CAPABILITY,
    LODY_SUBAGENTS_CANCEL_METHOD,
    LODY_SUBAGENTS_CAPABILITY,
    LODY_SUBAGENTS_LIST_METHOD,
    LODY_SUBAGENTS_OUTPUT_METHOD,
    MAX_FINISHED_SUBAGENT_RUNS,
    SubagentEventEmitter,
    SubagentRun,
    client_supports_subagent_events,
    json_safe,
    lody_task,
    lody_task_meta,
    snapshot_reason,
    summary_line,
    tail_lines,
)

if TYPE_CHECKING:
    from bub.builtin.agent import BubAgent
    from bub.builtin.subagents import SubagentEvent, SubagentTask

logger = logging.getLogger(__name__)

#: Client identity the Lody app sends in `InitializeRequest.clientInfo`:
#: `{name: "lody", title: "Lody", version: "1"}`. Matching is prefix-based on
#: `name` so variants such as `lody-cli` are still recognized.
_LODY_CLIENT_NAME = "lody"

_LODY_STEERING_CAPABILITY = {
    "version": 1,
    "transport": "request",
    "upstreamTurn": "same",
    "configPolicy": "active",
}

_LODY_EXTENSION_CAPABILITIES: dict[str, object] = {
    "steering": _LODY_STEERING_CAPABILITY,
    "sessionTitle": {"version": 1},
}
if BUB_SUBAGENT_TASKS:
    # Without Bub's subagent tasks there is nothing to observe, so advertising
    # either capability would only make the client wait for events that never come.
    _LODY_EXTENSION_CAPABILITIES["subagentEvents"] = dict(
        LODY_SUBAGENT_EVENTS_CAPABILITY
    )
    _LODY_EXTENSION_CAPABILITIES["subagents"] = dict(LODY_SUBAGENTS_CAPABILITY)

_SUBAGENT_REQUEST_METHODS = {
    LODY_SUBAGENTS_LIST_METHOD,
    LODY_SUBAGENTS_CANCEL_METHOD,
    LODY_SUBAGENTS_OUTPUT_METHOD,
}

#: How long a generated session title may take before it is abandoned. A title is
#: best effort: the client keeps its own draft when generation fails.
SESSION_TITLE_TIMEOUT_SECONDS = 30.0
SESSION_TITLE_MAX_CHARS = 80
SESSION_TITLE_INPUT_CHARS = 2000
_SESSION_TITLE_INSTRUCTION = (
    "Write a title for the coding session that starts with the user request below.\n"
    "Reply with the title only: at most 8 words, no quotes, no surrounding text, "
    "no markdown and no trailing punctuation.\n\n"
    "User request:\n{request}"
)

#: Quotation pairs a model may wrap a title in.
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "`": "`"}


def is_lody_client(client_info: Implementation | None) -> bool:
    """Whether this connection comes from the Lody client.

    Session titles are the only contract here that has no client-side capability
    to negotiate, so the client has to be recognized from `clientInfo` instead.
    An unknown or absent identity is not Lody.
    """

    name = getattr(client_info, "name", None)
    if isinstance(name, str) and name.strip().casefold().startswith(_LODY_CLIENT_NAME):
        return True
    title = getattr(client_info, "title", None)
    return isinstance(title, str) and title.strip().casefold() == _LODY_CLIENT_NAME


class LodyExtension:
    """Per-connection Lody state behind ``BubACPAgent``'s extension hooks."""

    def __init__(self, agent: BubACPAgent) -> None:
        self._agent = agent
        self.lody_client = False
        self._subagent_events_enabled = False
        self._subagent_runs: dict[str, SubagentRun] = {}
        self._subagent_task_runs: dict[str, str] = {}
        # Finished run IDs per session, oldest finish first, for retention.
        self._finished_subagent_runs: dict[str, deque[str]] = {}
        self._title_tasks: dict[str, asyncio.Task[None]] = {}
        self._title_sessions: set[str] = set()

    # -- ACPExtension hooks -------------------------------------------------

    def initialize(
        self,
        client_capabilities: ClientCapabilities | None,
        client_info: Implementation | None,
    ) -> dict[str, object] | None:
        # Subagent events are opt-in on both sides: publishing needs the client to
        # advertise the same version, whoever the client is.
        self._subagent_events_enabled = client_supports_subagent_events(
            client_capabilities
        )
        # `_meta.lody` is the Lody extension surface, so it is advertised only to
        # clients that identify themselves as Lody. Other clients see nothing
        # Lody-namespaced and never receive Lody-specific behavior such as
        # automatic session titles.
        self.lody_client = is_lody_client(client_info)
        if not self.lody_client:
            return None
        return {"lody": dict(_LODY_EXTENSION_CAPABILITIES)}

    def handles(self, method: str) -> bool:
        return method in _SUBAGENT_REQUEST_METHODS and BUB_SUBAGENT_TASKS

    async def ext_method(
        self, method: str, params: Mapping[str, object]
    ) -> dict[str, object]:
        return await self._subagent_request(method, params)

    def runtime_agent_created(
        self, runtime_agent: BubAgent, session: ACPSession
    ) -> None:
        """Publish this session's subagent tasks to the client, per negotiated capability."""

        if not (self._subagent_events_enabled or self.lody_client):
            return
        if not BUB_SUBAGENT_TASKS:
            return
        from bub.builtin.subagents import SubagentEvent

        async def handler(event: SubagentEvent) -> None:
            await self._on_subagent_event(session.session_id, event)

        runtime_agent.events.on(SubagentEvent, handler)

    def turn_finished(self, prompt: list[ACPPromptBlock], session: ACPSession) -> None:
        self._schedule_session_title(prompt, session)

    async def session_ended(self, session_id: str) -> None:
        """Stop a closed session's subagent tasks and forget its runs and title."""

        self._title_sessions.discard(session_id)
        if BUB_SUBAGENT_TASKS:
            from bub.builtin.subagents import subagent_manager

            await subagent_manager.cancel_session(
                _bub_session_id(self._agent.settings.channel_name, session_id)
            )
        for run_id, run in list(self._subagent_runs.items()):
            if run.session_id == session_id:
                self._subagent_runs.pop(run_id, None)
                self._subagent_task_runs.pop(run.task.task_id, None)
        self._finished_subagent_runs.pop(session_id, None)

    # -- Subagents ----------------------------------------------------------

    async def _on_subagent_event(self, session_id: str, event: SubagentEvent) -> None:
        from bub.builtin.subagents import (
            SubagentFinished,
            SubagentOutput,
            SubagentStarted,
        )

        run = self._subagent_run(session_id, event.task)
        if isinstance(event, SubagentStarted):
            if run.emitter is not None:
                await run.emitter.started(
                    description=event.task.description or None,
                    model_id=event.task.model,
                    name=event.task.profile,
                )
            await self._publish_subagent_lifecycle(run)
        elif isinstance(event, SubagentOutput):
            await self._publish_subagent_output(run, event.event)
        elif isinstance(event, SubagentFinished):
            await self._publish_subagent_end(run)

    def _subagent_run(self, session_id: str, task: SubagentTask) -> SubagentRun:
        run_id = self._subagent_task_runs.get(task.task_id)
        if run_id is not None and (run := self._subagent_runs.get(run_id)) is not None:
            return run
        emitter = (
            SubagentEventEmitter(self._agent._require_client(), session_id)
            if self._subagent_events_enabled
            else None
        )
        run = SubagentRun(
            run_id=emitter.run_id if emitter is not None else uuid4().hex,
            session_id=session_id,
            task=task,
            emitter=emitter,
        )
        self._subagent_runs[run.run_id] = run
        self._subagent_task_runs[task.task_id] = run.run_id
        return run

    async def _publish_subagent_output(
        self, run: SubagentRun, event: StreamEvent
    ) -> None:
        emitter = run.emitter
        if event.kind in ("text", "reasoning"):
            delta = str(event.data.get("delta", ""))
            if not delta:
                return
            if event.kind == "text":
                run.output.append(delta)
            if emitter is not None:
                await emitter.output(
                    _text_update(
                        "agent_message_chunk"
                        if event.kind == "text"
                        else "agent_thought_chunk",
                        delta,
                    )
                )
        elif event.kind == "tool_call":
            for call in _list_payload(event.data.get("tool_calls")):
                tool_id = _tool_call_id(run.next_tool_index, call)
                name = _tool_name(call)
                raw_input = _tool_raw_input(call)
                run.next_tool_index += 1
                title = _tool_title(name, raw_input)
                run.pending_tools.append((tool_id, title))
                if emitter is not None:
                    await emitter.output(
                        {
                            "sessionUpdate": "tool_call",
                            "toolCallId": tool_id,
                            "title": title,
                            "kind": _tool_kind(name),
                            "status": "in_progress",
                            "rawInput": json_safe(raw_input),
                        }
                    )
            if emitter is not None:
                await emitter.progress(**_subagent_progress(run.task))
        elif event.kind == "tool_result":
            results = _list_payload(event.data.get("tool_results"))
            for position, result in enumerate(results):
                if position < len(run.pending_tools):
                    tool_id, title = run.pending_tools[position]
                else:
                    tool_id, title = f"tool-{run.next_tool_index}", "tool"
                    run.next_tool_index += 1
                status, result = _tool_result_outcome(result)
                if emitter is not None:
                    await emitter.output(
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": tool_id,
                            "title": title,
                            "status": status,
                            "content": [
                                {
                                    "type": "content",
                                    "content": {
                                        "type": "text",
                                        "text": _stringify(result),
                                    },
                                }
                            ],
                        }
                    )
            run.pending_tools = []
        elif event.kind == "usage":
            run.reported_usage = _event_usage(event)

    async def _publish_subagent_end(self, run: SubagentRun) -> None:
        if run.terminated:
            return
        run.terminated = True
        self._prune_finished_subagent_runs(run)
        task = run.task
        if run.emitter is not None:
            # Progress counters are display observations, never usage accounting input.
            await run.emitter.progress(
                **_subagent_progress(task),
                totalTokens=_subagent_total_tokens(task.usage or run.reported_usage),
            )
            reason = snapshot_reason(task)
            await run.emitter.snapshot(
                task.status,
                description=task.description or None,
                model_id=task.model,
                name=task.profile,
                summary=summary_line(task.output)
                if task.status == "completed"
                else None,
                reason_code=reason[0] if reason else None,
                reason_message=reason[1] if reason else None,
            )
        await self._publish_subagent_lifecycle(run)

    def _prune_finished_subagent_runs(self, run: SubagentRun) -> None:
        """Record that ``run`` finished and keep only the session's newest finishes.

        Finished runs answer ``list`` and ``output``, but each one holds the Bub task
        (and with it the child's request state), so an open session must not keep
        every run it ever started. Retention follows completion order, so the run
        that just finished always survives, however early it started.
        """

        finished = self._finished_subagent_runs.setdefault(run.session_id, deque())
        finished.append(run.run_id)
        while len(finished) > MAX_FINISHED_SUBAGENT_RUNS:
            dropped = self._subagent_runs.pop(finished.popleft(), None)
            if dropped is not None:
                self._subagent_task_runs.pop(dropped.task.task_id, None)

    async def _publish_subagent_lifecycle(self, run: SubagentRun) -> None:
        """Tag the tool call that started the task with its `_meta.lody.task` lifecycle."""

        call_id = run.task.parent_call_id
        if not self.lody_client or not call_id:
            return
        meta = lody_task_meta(
            run, _subagent_total_tokens(run.task.usage or run.reported_usage)
        )
        try:
            await self._agent._require_client().session_update(
                run.session_id,
                ToolCallProgress(
                    tool_call_id=call_id, field_meta={"lody": {"task": meta}}
                ),
            )
        except Exception:
            # Observing a run must never break the run itself.
            logger.warning("Failed to publish a subagent task update", exc_info=True)

    async def _subagent_request(
        self, method: str, params: Mapping[str, object]
    ) -> dict[str, object]:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RequestError.invalid_params({"field": "sessionId"})
        if method == LODY_SUBAGENTS_LIST_METHOD:
            active_only = params.get("activeOnly", False)
            if not isinstance(active_only, bool):
                raise RequestError.invalid_params({"field": "activeOnly"})
            return {
                "tasks": [
                    lody_task(run)
                    for run in self._subagent_runs.values()
                    if run.session_id == session_id and (run.live or not active_only)
                ]
            }
        task_id = params.get("taskId")
        run = self._subagent_runs.get(task_id) if isinstance(task_id, str) else None
        if run is None or run.session_id != session_id:
            raise RequestError.invalid_params({"field": "taskId"})
        if method == LODY_SUBAGENTS_CANCEL_METHOD:
            if run.live:
                from bub.builtin.subagents import subagent_manager

                await subagent_manager.cancel(run.task.task_id)
            return {}
        tail = params.get("tail")
        if tail is not None and (
            not isinstance(tail, int) or isinstance(tail, bool) or tail < 1
        ):
            raise RequestError.invalid_params({"field": "tail"})
        return {"output": tail_lines("".join(run.output), tail)}

    # -- Session titles -----------------------------------------------------

    def _schedule_session_title(
        self, prompt: list[ACPPromptBlock], session: ACPSession
    ) -> None:
        """Start best-effort title generation for a session's first turn.

        Only Lody clients receive titles: the capability is a promise that the
        client can drop its own title generator for this session.
        """

        if not self.lody_client:
            return
        if session.session_id not in self._agent._sessions:
            return
        if session.title is not None or session.session_id in self._title_sessions:
            return
        request = _prompt_text(prompt)
        if not request:
            return

        self._title_sessions.add(session.session_id)
        task = asyncio.create_task(
            self._publish_session_title(session, request),
            name=f"acp-session-title-{session.session_id}",
        )
        self._title_tasks[session.session_id] = task
        task.add_done_callback(
            lambda _task, session_id=session.session_id: self._title_tasks.pop(
                session_id, None
            )
        )

    async def _publish_session_title(self, session: ACPSession, request: str) -> None:
        """Generate a title, persist it and push it to the client."""

        try:
            generated = await asyncio.wait_for(
                self._generate_session_title(session, request),
                SESSION_TITLE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A title is best effort: the client keeps its own draft.
            logger.warning(
                "Session title generation failed for %s: %s",
                session.session_id,
                error,
            )
            return

        title = _clean_title(generated)
        if not title or session.session_id not in self._agent._sessions:
            return
        session.title = title
        self._agent._save_sessions()
        client = self._agent._client
        if client is None:
            return
        try:
            await client.session_update(
                session.session_id,
                SessionInfoUpdate(
                    title=title, field_meta={"lody": {"titleSource": "generated"}}
                ),
            )
        except Exception as error:
            # The title is already persisted; a failed push must not strand the task.
            logger.warning(
                "Failed to push the session title for %s: %s",
                session.session_id,
                error,
            )

    async def _generate_session_title(self, session: ACPSession, request: str) -> str:
        """Ask the session's model for a short title using a throwaway tape."""

        agent = self._agent._runtime_agents.get(session.session_id)
        if agent is None:
            return ""
        title_session = f"temp/{uuid4().hex[:8]}"
        state: dict[str, Any] = {
            "_runtime_agent": agent,
            "_runtime_workspace": str(session.cwd),
            "session_id": title_session,
        }
        prompt = _SESSION_TITLE_INSTRUCTION.format(
            request=request[:SESSION_TITLE_INPUT_CHARS]
        )
        parts: list[str] = []
        # A title needs one completion, not the session's tool surface or skills.
        stream = await agent.run_stream(
            session_id=title_session,
            prompt=prompt,
            state=state,
            model=session.runtime.get("model"),
            allowed_tools=set(),
            allowed_skills=set(),
        )
        async with contextlib.aclosing(stream):
            async for event in stream:
                if event.kind == "text":
                    parts.append(str(event.data.get("delta", "")))
        return "".join(parts)


def _prompt_text(prompt: list[ACPPromptBlock]) -> str:
    """Return the concatenated text blocks of one prompt."""

    parts = [
        str(_block_value(block, "text", ""))
        for block in prompt
        if _block_type(block) == "text"
    ]
    return "\n".join(part for part in parts if part).strip()


def _text_update(session_update: str, text: str) -> dict[str, object]:
    return {
        "sessionUpdate": session_update,
        "content": {"type": "text", "text": text},
    }


def _tool_title(name: str, raw_input: object) -> str:
    """Human-readable tool title, matching the live stream's bash handling."""

    if name == "bash":
        command = _block_value(raw_input, "command")
        if isinstance(command, str) and command:
            return command
    return name


def _subagent_progress(task: SubagentTask) -> dict[str, Any]:
    started = task.started_at or task.created_at
    return {
        "lastToolName": task.last_tool,
        "turnCount": task.steps,
        "toolCallCount": task.tool_calls,
        "durationMs": int(((task.ended_at or time.time()) - started) * 1000),
    }


def _subagent_total_tokens(usage: object) -> int | None:
    if not isinstance(usage, Mapping):
        return None
    return _usage_total_tokens(usage)


def _clean_title(value: str) -> str:
    """Normalize a generated title into a single short line."""

    title = " ".join(str(value).split()).strip()
    if not title:
        return ""
    if title.casefold().startswith("title:"):
        title = title[len("title:") :].strip()
    if len(title) > 1 and _QUOTE_PAIRS.get(title[0]) == title[-1]:
        title = title[1:-1].strip()
    return title[:SESSION_TITLE_MAX_CHARS].strip()
