"""Protocol sessions and run ownership, independent of the HTTP transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import jsonschema
from ag_ui.core import RunAgentInput
from bub.builtin.agent import BubAgent
from bub.core.tape import Tape, TapeEntry, session_tape_name, to_message

from bub_ag_ui.events import EventStream, result_text, wire_event
from bub_ag_ui.execution import (
    AGUIInboundMessage,
    StreamRouter,
    install_execution_hooks,
)
from bub_ag_ui.history import AGUIHistory, HistoryView, RunEvents
from bub_ag_ui.lifecycle import committed
from bub_ag_ui.state import SharedState
from bub_ag_ui.tools import ExternalTools, InterruptTool

logger = logging.getLogger(__name__)


class ProtocolError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def fingerprint(request: RunAgentInput) -> str:
    value = request.model_dump(mode="json", by_alias=True)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def model_message(message: dict[str, Any]) -> dict[str, Any] | None:
    role = message["role"]
    if role not in {"user", "assistant", "tool"}:
        return None
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for part in content:
            if part["type"] == "text":
                parts.append({"type": "text", "text": part["text"]})
            elif part["type"] in {"image", "audio", "video"}:
                source = part.get("source", {})
                mime = part.get("mimeType") or source.get("mimeType")
                if not mime:
                    raise ProtocolError("Media input requires mimeType")
                data = {"type": part["type"], "media_type": mime}
                if source.get("type") == "url":
                    data["url"] = source["value"]
                elif source.get("type") == "data":
                    data["data"] = source["value"]
                else:
                    raise ProtocolError("Media input requires a URL or data source")
                parts.append(data)
            else:
                raise ProtocolError(
                    f"Bub 0.6.1 cannot consume {part['type']} input", 422
                )
        content = parts
    result = {"role": role, "content": content}
    if role == "assistant" and message.get("toolCalls"):
        result["tool_calls"] = deepcopy(message["toolCalls"])
    if role == "tool":
        result["tool_call_id"] = message["toolCallId"]
        if message.get("error"):
            result["content"] = result_text(
                {"error": message["error"], "content": content}
            )
    # Validate conversion before accepting the run, not after the first SSE byte.
    to_message(result)
    return result


@dataclass
class Thread:
    """Execution synchronization only; durable state belongs to the mounted tape."""

    tape: Tape
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    admission: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def sidecar(self) -> AGUIHistory:
        return self.tape.get_sidecar("ag-ui")

    async def read(self) -> HistoryView:
        return await self.sidecar.read(self.tape)

    async def history(self) -> dict[str, Any]:
        return (await self.read()).as_dict()

    async def events(self, run_id: str, after: int = 0) -> RunEvents:
        return await self.sidecar.events(self.tape, run_id, after)

    async def append(self, kind: str, **data: Any) -> None:
        async with self.changed:

            async def write() -> None:
                await self.sidecar.append(self.tape, kind, **data)
                self.changed.notify_all()

            await committed(write())

    async def checkpoint(self, pending: list[dict[str, Any]], model_tape: str) -> None:
        await self.append("checkpoint", pending=pending, model_tape=model_tape)


@dataclass(frozen=True)
class Submission:
    thread: Thread
    created: bool


@dataclass(frozen=True)
class RunningRun:
    run_id: str
    task: asyncio.Task[None]
    started: asyncio.Event

    async def cancel(self) -> None:
        # Cancellation must enter the producer's cleanup boundary, even before
        # its first token. No lock is handed from the HTTP task to the producer.
        await self.started.wait()
        if not self.task.done() and not self.task.cancelling():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class AGUIAdapter:
    """One single-writer controller per configured store/workspace.

    A run task owns execution; subscribers follow the mounted history tape. Abort
    cancels execution, not merely the HTTP response. Replaying a run never invokes
    the model or its tools again. Server restart marks unfinished runs failed.
    """

    def __init__(
        self,
        framework: Any,
        *,
        workspace: Path | None = None,
        interrupts: tuple[InterruptTool, ...] = (),
        agent_factory: Callable[..., BubAgent] = BubAgent,
        max_run_seconds: float = 600,
        bind_router: bool = True,
    ) -> None:
        self.framework = framework
        install_execution_hooks(framework)
        if bind_router:
            framework.bind_channel_router(StreamRouter())
        self.workspace = (workspace or Path.cwd()).resolve()
        self.interrupts = interrupts
        self.agent_factory = agent_factory
        self.max_run_seconds = max_run_seconds
        self._threads: dict[tuple[str, str], Thread] = {}
        self._runs: dict[str, RunningRun] = {}
        self._closed = False
        self._loading = asyncio.Lock()
        self._storage_agent = agent_factory(framework)

    def session_id(self, principal: str, thread_id: str) -> str:
        identity = json.dumps([principal, thread_id], ensure_ascii=False)
        return "ag-ui:" + hashlib.sha256(identity.encode()).hexdigest()

    async def thread(self, principal: str, thread_id: str) -> Thread:
        if not principal or not thread_id or len(thread_id) > 512:
            raise ProtocolError("Invalid principal/threadId")
        key = principal, thread_id
        async with self._loading:
            if key not in self._threads:
                name = session_tape_name(self.session_id(*key), self.workspace)
                thread = Thread(self._storage_agent.tape(name))
                await self._recover(thread)
                self._threads[key] = thread
            return self._threads[key]

    async def history(self, principal: str, thread_id: str) -> dict[str, Any]:
        thread = await self.thread(principal, thread_id)
        return (await thread.read()).as_dict()

    async def _recover(self, thread: Thread) -> None:
        for run_id, run in list((await thread.read()).runs.items()):
            if run["done"]:
                continue
            # Recovery must fail closed if storage is unavailable; unlike a
            # live producer, there is no owning task to report an interrupted run.
            await self._finish_failed_run(
                thread,
                run_id,
                "Server stopped before this run completed; it will not be executed again",
                "INTERRUPTED",
            )

    async def _close_abandoned(self, thread: Thread, run_id: str) -> None:
        opened: dict[tuple[str, str], str] = {}
        steps: set[str] = set()
        for record in (await thread.events(run_id)).records:
            event = record["event"]
            kind = event["type"]
            if kind == "STEP_STARTED":
                steps.add(event["stepName"])
            elif kind == "STEP_FINISHED":
                steps.discard(event["stepName"])
            for prefix, id_key in [
                ("TEXT_MESSAGE", "messageId"),
                ("REASONING_MESSAGE", "messageId"),
                ("REASONING", "messageId"),
                ("TOOL_CALL", "toolCallId"),
            ]:
                if kind == prefix + "_START":
                    opened[prefix, event[id_key]] = id_key
                elif kind == prefix + "_END":
                    opened.pop((prefix, event[id_key]), None)
        for (prefix, id), key in reversed(list(opened.items())):
            await thread.append(
                "event", runId=run_id, event=wire_event(prefix + "_END", **{key: id})
            )
        for step in steps:
            await thread.append(
                "event", runId=run_id, event=wire_event("STEP_FINISHED", stepName=step)
            )

    async def _write_messages(self, name: str, messages: list[dict[str, Any]]) -> None:
        tape = self._storage_agent.tape(name)
        await tape.ensure_bootstrap_anchor()
        for message in messages:
            if converted := model_message(message):
                await tape.store.append(
                    tape.name,
                    TapeEntry.message(converted, ag_ui_message_id=message["id"]),
                )

    def _reconcile(
        self, request: RunAgentInput, view: HistoryView
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        incoming = [
            message.model_dump(mode="json", by_alias=True)
            for message in request.messages
        ]
        if len({message["id"] for message in incoming}) != len(incoming):
            raise ProtocolError("Duplicate message IDs")
        existing = {message["id"]: message for message in view.projection.messages}
        pending = {call["id"]: call for call in view.pending}
        additions: list[dict[str, Any]] = []
        seen_calls: set[str] = set()
        branch = False
        edited: set[str] = set()
        for message in incoming:
            old = existing.get(message["id"])
            if old is not None:
                # Echoed history is not an update: the stored message is authoritative.
                # Only user edits participate in creating a new context branch.
                if (
                    old["role"] == message["role"] == "user"
                    and old["content"] != message["content"]
                ):
                    branch = True
                    edited.add(message["id"])
                continue
            if message["role"] == "tool":
                call_id = message["toolCallId"]
                if (
                    call_id not in pending
                    or pending[call_id]["kind"] != "frontend"
                    or call_id in seen_calls
                ):
                    raise ProtocolError(
                        "Unknown, duplicate, or non-frontend tool result", 409
                    )
                seen_calls.add(call_id)
            elif message["role"] != "user":
                raise ProtocolError(
                    "Only user input and results of pending frontend calls may be appended",
                    409,
                )
            additions.append(message)
        if request.resume:
            for resume in request.resume:
                call = pending.get(resume.interrupt_id)
                if (
                    call is None
                    or call["kind"] != "interrupt"
                    or call["id"] in seen_calls
                ):
                    raise ProtocolError("Unknown or duplicate interrupt", 409)
                if resume.status == "resolved" and call.get("response_schema"):
                    try:
                        jsonschema.validate(resume.payload, call["response_schema"])
                    except jsonschema.ValidationError as exc:
                        raise ProtocolError(
                            "Resume payload does not match responseSchema", 422
                        ) from exc
                seen_calls.add(call["id"])
                additions.append(
                    {
                        "id": str(uuid4()),
                        "role": "tool",
                        "toolCallId": call["id"],
                        "content": result_text(
                            resume.payload
                            if resume.status == "resolved"
                            else {"cancelled": True}
                        ),
                    }
                )
        if pending:
            if branch or (
                any(message["role"] == "user" for message in additions)
                and seen_calls != pending.keys()
            ):
                raise ProtocolError(
                    "Resolve pending tools/interrupts before sending a new user turn",
                    409,
                )
            if not seen_calls:
                raise ProtocolError(
                    "Expected a pending tool result or resume entry", 409
                )
        if pending:
            additions = [
                message for message in additions if message["role"] == "tool"
            ] + [message for message in additions if message["role"] == "user"]
        # The AG-UI runtime sends a truncated history for edit/regenerate. Keep
        # old tapes immutable, and build a new context generation from server data.
        if (
            not pending
            and incoming
            and incoming[-1]["role"] == "user"
            and existing.get(incoming[-1]["id"], {}).get("role") == "user"
        ):
            target = incoming[-1]
            index = next(
                i
                for i, m in enumerate(view.projection.messages)
                if m["id"] == target["id"]
            )
            if branch or not additions:
                if additions or (edited and edited != {target["id"]}):
                    raise ProtocolError(
                        "Only the final user message may be edited", 409
                    )
                model_message(target)
                return (
                    deepcopy(view.projection.messages[:index]) + [target],
                    [],
                    True,
                )
        if branch:
            raise ProtocolError(
                "An edited user message must be the final request message", 409
            )
        if not additions:
            raise ProtocolError("No new user message or pending tool result", 409)
        for message in additions:
            try:
                model_message(message)
            except (ValueError, TypeError) as exc:
                raise ProtocolError("Invalid message content", 422) from exc
        return (
            additions,
            [call for id, call in pending.items() if id not in seen_calls],
            False,
        )

    async def submit(self, principal: str, request: RunAgentInput) -> Submission:
        if not request.run_id or len(request.run_id) > 512:
            raise ProtocolError("Invalid runId")
        thread = await self.thread(principal, request.thread_id)
        async with thread.admission:
            if self._closed:
                raise ProtocolError("Adapter is shutting down", 503)
            view = await thread.read()
            digest = fingerprint(request)
            if run := view.runs.get(request.run_id):
                if run["fingerprint"] != digest:
                    raise ProtocolError(
                        "runId already belongs to a different request", 409
                    )
                return Submission(thread, created=False)
            if thread.tape.name in self._runs:
                raise ProtocolError("Another run owns this thread", 409)
            additions, remaining, branch = self._reconcile(request, view)
            for tool in request.tools:
                try:
                    jsonschema.Draft202012Validator.check_schema(tool.parameters)
                except jsonschema.SchemaError as exc:
                    raise ProtocolError("Invalid frontend tool schema", 422) from exc
            agent = self.agent_factory(
                self.framework,
                tape_store=thread.tape.store,
            )
            # Admission is validation only. Framework hooks run exactly once,
            # inside process_inbound, and never for a rejected/replayed request.
            try:
                external = ExternalTools([*request.tools, *self.interrupts])
                external.validate(agent)
            except ValueError as exc:
                raise ProtocolError(str(exc), 422) from exc
            try:
                await thread.append("admit", runId=request.run_id, fingerprint=digest)
            except BaseException:
                # A cancelled append may have committed before propagating the
                # cancellation. Close that admission before releasing the gate.
                if request.run_id in (await thread.read()).runs:
                    await self._fail(
                        thread,
                        request.run_id,
                        "Run cancelled during admission",
                        "CANCELLED",
                    )
                raise
            started = asyncio.Event()
            task = asyncio.create_task(
                self._execute(
                    thread,
                    request,
                    agent,
                    additions,
                    remaining,
                    branch,
                    self.session_id(principal, request.thread_id),
                    external,
                    started,
                ),
                name=f"ag-ui:{request.run_id}",
            )
            self._runs[thread.tape.name] = RunningRun(request.run_id, task, started)
        return Submission(thread, created=True)

    async def _execute(
        self,
        thread: Thread,
        request: RunAgentInput,
        agent: BubAgent,
        additions: list[dict[str, Any]],
        remaining: list[dict[str, Any]],
        branch: bool,
        session_id: str,
        external: ExternalTools,
        started: asyncio.Event,
    ) -> None:
        async def emit(event: dict[str, Any]) -> None:
            await thread.append("event", runId=request.run_id, event=event)

        stream = EventStream(emit)
        try:
            started.set()
            model_tape = (await thread.read()).model_tape
            async with asyncio.timeout(self.max_run_seconds):
                await stream.send(
                    "RUN_STARTED",
                    threadId=request.thread_id,
                    runId=request.run_id,
                    parentRunId=request.parent_run_id,
                )
                if branch:
                    model_tape = model_tape + ":branch:" + str(uuid4())
                    messages = additions
                else:
                    messages = [*(await thread.read()).projection.messages, *additions]

                async def prepare() -> None:
                    await stream.send("MESSAGES_SNAPSHOT", messages=messages)
                    await self._write_messages(model_tape, additions)
                    await thread.checkpoint(remaining, model_tape)

                await committed(prepare())
                shared = SharedState(
                    request.state
                    if request.state is not None
                    else (await thread.read()).projection.state,
                    emit,
                )
                await shared.set(shared.value)
                if not remaining:
                    inbound = AGUIInboundMessage(
                        session_id=session_id,
                        channel="ag-ui",
                        chat_id=request.thread_id,
                        content=next(
                            (
                                m["content"]
                                for m in reversed(additions)
                                if m["role"] == "user"
                                and isinstance(m.get("content"), str)
                            ),
                            "",
                        ),
                        is_active=True,
                        _runtime_agent=agent,
                        request=request,
                        model_tape=model_tape,
                        workspace=self.workspace,
                        shared=shared,
                        events=stream,
                        external=external,
                    )
                    await self.framework.process_inbound(inbound, stream_output=True)
                    if not inbound.stream_complete:
                        raise RuntimeError(
                            "AG-UI stream did not complete; check channel configuration"
                        )
                    # Native stream cleanup and save/render/dispatch hooks have
                    # completed before the controller checkpoints and finishes.
                    await thread.checkpoint(external.pending, model_tape)
                await stream.close()
                interrupts = [
                    {
                        "id": call["id"],
                        "toolCallId": call["id"],
                        "reason": call["reason"],
                        "responseSchema": call.get("response_schema"),
                        "metadata": {
                            "arguments": json.loads(call["arguments"]),
                            "toolName": call["name"],
                        },
                    }
                    for call in (await thread.read()).pending
                    if call["kind"] == "interrupt"
                ]
                fields: dict[str, Any] = {}
                if interrupts:
                    fields["outcome"] = {"type": "interrupt", "interrupts": interrupts}
                await stream.send(
                    "RUN_FINISHED",
                    threadId=request.thread_id,
                    runId=request.run_id,
                    **fields,
                )
        except asyncio.CancelledError:
            # A cancellation racing the terminal append may arrive after that
            # write committed. Never publish a second terminal outcome.
            await self._fail(thread, request.run_id, "Run cancelled", "CANCELLED")
        except Exception:
            logger.exception("AG-UI run failed: %s", request.run_id)
            await self._fail(
                thread,
                request.run_id,
                "Agent run failed; see server logs",
                "RUN_FAILED",
            )
        finally:
            self._runs.pop(thread.tape.name, None)
            async with thread.changed:
                thread.changed.notify_all()

    async def _fail(self, thread: Thread, run_id: str, message: str, code: str) -> None:
        try:
            await self._finish_failed_run(thread, run_id, message, code)
        except Exception:
            logger.exception("Unable to persist AG-UI failure; run remains interrupted")

    async def _finish_failed_run(
        self, thread: Thread, run_id: str, message: str, code: str
    ) -> None:
        view = await thread.read()
        if view.runs.get(run_id, {}).get("done"):
            return
        await thread.checkpoint([], view.model_tape)
        await self._close_abandoned(thread, run_id)
        # Close protocol boundaries before the terminal commit; never reconstruct
        # model context or invent tool results from display-only events.
        # Restart retries unfinished protocol cleanup;
        # cancellation racing a committed terminal outcome cannot append another.
        await thread.append(
            "event",
            runId=run_id,
            event=wire_event("RUN_ERROR", message=message, code=code),
        )

    async def events(
        self, thread: Thread, run_id: str, after: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        cursor = after
        while True:
            async with thread.changed:
                batch = await thread.events(run_id, cursor)
                cursor = batch.cursor
                if not batch.admitted:
                    raise ProtocolError(
                        "Run history was reset; reload thread history", 409
                    )
                running = self._runs.get(thread.tape.name)
                done = batch.done or running is None or running.run_id != run_id
                if not batch.records and not done:
                    await thread.changed.wait()
                    continue
            for record in batch.records:
                yield record
            if done:
                return

    async def cancel(self, principal: str, thread_id: str, run_id: str) -> bool:
        thread = await self.thread(principal, thread_id)
        running = self._runs.get(thread.tape.name)
        if running is None or running.run_id != run_id:
            return False
        await running.cancel()
        return True

    async def close(self) -> None:
        self._closed = True
        # Finish in-flight admissions before taking the shutdown snapshot.
        for thread in list(self._threads.values()):
            async with thread.admission:
                pass
        await asyncio.gather(*(run.cancel() for run in list(self._runs.values())))
