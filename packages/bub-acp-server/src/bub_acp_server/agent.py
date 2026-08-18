from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import bub
from acp import (
    run_agent,
    text_block,
    update_agent_message_text,
    update_agent_thought_text,
    update_user_message,
    update_user_message_text,
)
from acp.helpers import (
    start_tool_call,
    tool_content,
    tool_terminal_ref,
    update_tool_call,
)
from acp.interfaces import Client
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AudioContentBlock,
    ClientCapabilities,
    CloseSessionResponse,
    EmbeddedResourceContentBlock,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpServerStdio,
    NewSessionResponse,
    PromptResponse,
    ResourceContentBlock,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionInfo,
    SessionListCapabilities,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    SseMcpServer,
    TextContentBlock,
    ToolKind,
    UsageUpdate,
)
from bub.channels.message import ChannelMessage, MediaItem, MediaType
from bub.envelope import Envelope, content_of, field_of
from bub.model_selection import ModelChoice, ModelOptions
from bub.streaming import StreamEvent
from bub.tape import TapeEntry, TapeQuery
from bub.turn import TurnResult
from pydantic import TypeAdapter, ValidationError

from bub_acp_server.client_tools import ACPClientToolRuntime, replace_builtin_tools
from bub_acp_server.config import ACPServerSettings
from bub_acp_server.steering import ACPSteeringInbox

if TYPE_CHECKING:
    from bub.framework import BubFramework

type ACPPromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)
type ACPMcpServer = HttpMcpServer | SseMcpServer | McpServerStdio
type StreamPayload = Mapping[str, object]

REASONING_EFFORT_CONFIG_ID = "reasoning_effort"
REASONING_EFFORT_OPTIONS = (
    ("auto", "Auto"),
    ("none", "None"),
    ("minimal", "Minimal"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "Extra high"),
)

_PROMPT_ADAPTER = TypeAdapter(list[ACPPromptBlock])

logger = logging.getLogger(__name__)

SESSION_STEERING_METHOD = "session/steering"

_BUB_PROMPT_CONTEXT = re.compile(
    r"^(?=[^\n]*channel=\$)(?=[^\n]*chat_id=)[^\n]+\n"
    r"---Date: [^\n]+---\n",
    re.MULTILINE,
)
_CONTINUATION_PROMPT_PREFIX = "Continue the task until all targets are completed."


@dataclass(slots=True)
class ACPPromptRun:
    session_id: str
    started: asyncio.Event = field(default_factory=asyncio.Event)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    background: bool = False
    task: asyncio.Task[PromptResponse] | None = None


@dataclass(slots=True)
class ACPSession:
    session_id: str
    cwd: Path
    additional_directories: list[str] = field(default_factory=list)
    runtime: dict[str, str] = field(default_factory=dict)
    title: str | None = None
    updated_at: str | None = None

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()

    def info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.session_id,
            cwd=str(self.cwd),
            additional_directories=self.additional_directories or None,
            title=self.title,
            updated_at=self.updated_at,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "additional_directories": list(self.additional_directories),
            "runtime": dict(self.runtime),
            "title": self.title,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> ACPSession | None:
        session_id = data.get("session_id")
        cwd = data.get("cwd")
        if not isinstance(session_id, str) or not session_id:
            return None
        if not isinstance(cwd, str) or not cwd:
            return None

        additional_directories = data.get("additional_directories")
        if not isinstance(additional_directories, list):
            additional_directories = []

        title = data.get("title")
        updated_at = data.get("updated_at")
        runtime = data.get("runtime")
        if not isinstance(runtime, Mapping):
            runtime = {}
        return cls(
            session_id=session_id,
            cwd=Path(cwd).expanduser().resolve(),
            additional_directories=[
                str(item) for item in additional_directories if isinstance(item, str)
            ],
            runtime={
                str(key): str(value)
                for key, value in runtime.items()
                if isinstance(key, str)
            },
            title=title if isinstance(title, str) else None,
            updated_at=updated_at if isinstance(updated_at, str) else None,
        )


@dataclass(slots=True)
class ACPStreamState:
    tool_ids: dict[int, str] = field(default_factory=dict)
    pending_tool_indices: list[int] = field(default_factory=list)
    pending_terminal_calls: list[tuple[str | None, int]] = field(default_factory=list)
    terminal_tool_indices: set[int] = field(default_factory=set)
    context_compaction_indices: set[int] = field(default_factory=set)
    next_tool_index: int = 0
    sent_text: bool = False
    reported_usage: tuple[int, int] | None = None


class ACPStreamRouter:
    def __init__(self, client: Client, *, context_window_size: int = 128_000) -> None:
        self._client = client
        self._context_window_size = context_window_size
        self._stream_states: dict[str, ACPStreamState] = {}

    def wrap_stream(
        self, message: Envelope, stream: AsyncIterable[StreamEvent]
    ) -> AsyncIterable[StreamEvent]:
        session_id = _message_chat_id(message)
        state = ACPStreamState()
        self._stream_states[session_id] = state

        async def iterator() -> AsyncIterator[StreamEvent]:
            try:
                async for event in stream:
                    await self.publish_event(session_id, event)
                    await self._publish_usage_if_changed(
                        session_id,
                        state,
                        _event_usage(event) or getattr(stream, "usage", None),
                    )
                    yield event
            finally:
                await self._publish_usage_if_changed(
                    session_id, state, getattr(stream, "usage", None)
                )

        return iterator()

    def pop_stream_state(self, session_id: str) -> ACPStreamState | None:
        return self._stream_states.pop(session_id, None)

    async def dispatch_output(self, message: Envelope) -> bool:
        if field_of(message, "kind") == "error":
            await self._send_agent_text(_message_chat_id(message), content_of(message))
        return True

    async def quit(self, session_id: str) -> None:
        del session_id

    async def publish_event(self, session_id: str, event: StreamEvent) -> None:
        state = self._stream_states.setdefault(session_id, ACPStreamState())
        if event.kind == "text":
            delta = str(event.data.get("delta", ""))
            if delta:
                state.sent_text = True
                await self._send_agent_text(session_id, delta)
        elif event.kind == "reasoning":
            delta = str(event.data.get("delta", ""))
            if delta:
                await self._send_agent_thought(session_id, delta)
        elif event.kind == "user_text":
            delta = str(event.data.get("delta", ""))
            if delta:
                await self._send_user_text(session_id, delta)
        elif event.kind == "tool_call":
            await self._send_tool_calls(session_id, event.data)
        elif event.kind == "tool_result":
            await self._send_tool_results(session_id, event.data)
        elif event.kind == "error":
            message = (
                event.data.get("message") or event.data.get("error") or "unknown error"
            )
            await self._send_agent_text(session_id, f"\nError: {message}")

    async def _publish_usage_if_changed(
        self,
        session_id: str,
        state: ACPStreamState,
        usage: object,
    ) -> None:
        if not isinstance(usage, Mapping):
            return

        used = _usage_total_tokens(usage)
        if used is None:
            used = 0
        reported_size = _usage_context_window_size(usage)
        size = max(used, reported_size or self._context_window_size)
        snapshot = (used, size)
        if snapshot == state.reported_usage:
            return

        state.reported_usage = snapshot
        await self._client.session_update(
            session_id,
            UsageUpdate(session_update="usage_update", size=size, used=used),
        )

    async def _send_agent_text(self, session_id: str, text: str) -> None:
        if not text:
            return
        await self._client.session_update(session_id, update_agent_message_text(text))

    async def _send_agent_thought(self, session_id: str, text: str) -> None:
        if not text:
            return
        await self._client.session_update(session_id, update_agent_thought_text(text))

    async def _send_user_text(self, session_id: str, text: str) -> None:
        if not text:
            return
        await self._client.session_update(session_id, update_user_message_text(text))

    async def _send_tool_calls(self, session_id: str, data: StreamPayload) -> None:
        state = self._stream_states[session_id]
        state.pending_terminal_calls = []
        if "tool_calls" not in data:
            index = await self._send_tool_call(session_id, data)
            state.pending_tool_indices = [index]
            return

        calls = _list_payload(data.get("tool_calls"))
        state.pending_tool_indices = []
        for call in calls:
            index = await self._send_tool_call(session_id, {"call": call})
            state.pending_tool_indices.append(index)

    async def _send_tool_call(self, session_id: str, data: StreamPayload) -> int:
        state = self._stream_states[session_id]
        index = _int_value(data.get("index"), default=state.next_tool_index)
        state.next_tool_index = max(state.next_tool_index, index + 1)
        call = data.get("call")
        tool_id = _tool_call_id(index, call)
        state.tool_ids[index] = tool_id
        tool_name = _tool_name(call)
        is_context_compaction = tool_name == "tape.handoff"
        title = "Context compacting" if is_context_compaction else _tool_title(call)
        if tool_name == "bash":
            state.pending_terminal_calls.append((_tool_command(call), index))
        if is_context_compaction:
            state.context_compaction_indices.add(index)
        update = start_tool_call(
            tool_id,
            title,
            kind="other" if is_context_compaction else _tool_kind(tool_name),
            status="in_progress",
            raw_input=_tool_raw_input(call),
        )
        if is_context_compaction:
            update.field_meta = {"contextCompaction": True}
        await self._client.session_update(session_id, update)
        return index

    async def attach_terminal(
        self, session_id: str, command: str, terminal_id: str
    ) -> None:
        state = self._stream_states.get(session_id)
        if state is None or not state.pending_terminal_calls:
            return

        position = next(
            (
                position
                for position, (pending_command, _) in enumerate(
                    state.pending_terminal_calls
                )
                if pending_command == command
            ),
            0,
        )
        _, index = state.pending_terminal_calls.pop(position)
        tool_id = state.tool_ids[index]
        state.terminal_tool_indices.add(index)
        await self._client.session_update(
            session_id,
            update_tool_call(
                tool_id,
                status="in_progress",
                content=[tool_terminal_ref(terminal_id)],
            ),
        )

    async def _send_tool_results(self, session_id: str, data: StreamPayload) -> None:
        state = self._stream_states[session_id]
        if "tool_results" not in data:
            await self._send_tool_result(session_id, data)
            state.pending_tool_indices = []
            return

        results = _list_payload(data.get("tool_results"))
        for position, result in enumerate(results):
            if position < len(state.pending_tool_indices):
                index = state.pending_tool_indices[position]
            else:
                index = state.next_tool_index
                state.next_tool_index += 1
            await self._send_tool_result(session_id, {"index": index, "result": result})
        state.pending_tool_indices = []

    async def _send_tool_result(self, session_id: str, data: StreamPayload) -> None:
        state = self._stream_states[session_id]
        index = _int_value(data.get("index"), default=0)
        tool_id = state.tool_ids.get(index, f"tool-{index}")
        result = data.get("result")
        content = None
        is_context_compaction = index in state.context_compaction_indices
        if index not in state.terminal_tool_indices and not is_context_compaction:
            content = [tool_content(text_block(_stringify(result)))]
        update = update_tool_call(
            tool_id,
            title="Context compacted" if is_context_compaction else None,
            status="completed",
            raw_output=result,
            content=content,
        )
        if is_context_compaction:
            update.field_meta = {"contextCompaction": True}
        await self._client.session_update(session_id, update)


class BubACPAgent:
    def __init__(
        self,
        framework: BubFramework,
        *,
        client_tools: ACPClientToolRuntime | None = None,
        steering_inbox: ACPSteeringInbox | None = None,
    ) -> None:
        self.framework = framework
        self.settings = bub.ensure_config(ACPServerSettings)
        self.client_tools = client_tools or ACPClientToolRuntime()
        self._client: Client | None = None
        self._stream_router: ACPStreamRouter | None = None
        self._session_store_path = bub.home.expanduser() / "acp-sessions.json"
        self._sessions: dict[str, ACPSession] = self._load_sessions()
        self._prompt_lock = asyncio.Lock()
        self._steering_inbox = steering_inbox or ACPSteeringInbox()
        self._prompt_runs: dict[str, deque[ACPPromptRun]] = {}
        self._steering_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[PromptResponse]] = set()
        self._closing_sessions: set[str] = set()

    def set_steering_inbox(self, steering_inbox: ACPSteeringInbox) -> None:
        self._steering_inbox = steering_inbox

    def on_connect(self, conn: Client) -> None:
        self._client = conn
        self.client_tools.connect(conn)
        self._stream_router = ACPStreamRouter(
            conn, context_window_size=self.settings.context_window_size
        )
        self.client_tools.set_terminal_observer(self._stream_router.attach_terminal)
        self.framework.bind_channel_router(self._stream_router)

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        import importlib.metadata

        try:
            bub_version = importlib.metadata.version("bub")
        except importlib.metadata.PackageNotFoundError:
            bub_version = "0.0.0"
        del client_info, kwargs
        self.client_tools.set_capabilities(client_capabilities)
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_info=Implementation(name="bub", title="Bub", version=bub_version),
            field_meta={"steering": {"supported": True}},
            agent_capabilities=AgentCapabilities(
                load_session=True,
                session_capabilities=SessionCapabilities(
                    close=SessionCloseCapabilities(),
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities(),
                ),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[ACPMcpServer] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        del mcp_servers, kwargs
        session_id = uuid4().hex
        session = ACPSession(
            session_id=session_id,
            cwd=Path(cwd).expanduser().resolve(),
            additional_directories=list(additional_directories or []),
        )
        session.touch()
        self._sessions[session_id] = session
        self._save_sessions()
        return NewSessionResponse(
            session_id=session_id,
            config_options=await self._session_config_options(session),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[ACPMcpServer] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        del mcp_servers, kwargs
        session = self._load_or_adopt_session(
            session_id=session_id,
            cwd=cwd,
            additional_directories=additional_directories,
        )
        await self._attach_session_history(session)
        return LoadSessionResponse(
            config_options=await self._session_config_options(session)
        )

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[ACPMcpServer] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        del mcp_servers, kwargs
        session = self._load_or_adopt_session(
            session_id=session_id,
            cwd=cwd,
            additional_directories=additional_directories,
        )
        return ResumeSessionResponse(
            config_options=await self._session_config_options(session)
        )

    async def list_sessions(
        self,
        additional_directories: list[str] | None = None,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        del additional_directories, cursor, cwd, kwargs
        self._sessions = self._load_sessions()
        sessions = sorted(
            self._sessions.values(),
            key=lambda item: item.updated_at or "",
            reverse=True,
        )
        return ListSessionsResponse(sessions=[session.info() for session in sessions])

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> CloseSessionResponse | None:
        del kwargs
        self._closing_sessions.add(session_id)
        try:
            self._sessions.pop(session_id, None)
            self._save_sessions()
            run = self._current_prompt_run(session_id)
            if (
                run is not None
                and run.background
                and not run.started.is_set()
                and run.task is not None
            ):
                run.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run.task
            return CloseSessionResponse()
        finally:
            self._steering_locks.pop(session_id, None)
            self._closing_sessions.discard(session_id)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        await self.framework.quit_via_channel_router(session_id)

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **kwargs: Any,
    ) -> SetSessionConfigOptionResponse:
        del kwargs
        session = self._sessions.get(session_id) or self._adopt_session(session_id)
        session.touch()
        config_options = await self._set_session_config_option(
            session, config_id, value
        )
        self._save_sessions()
        return SetSessionConfigOptionResponse(config_options=config_options)

    async def prompt(
        self,
        prompt: list[ACPPromptBlock],
        session_id: str,
        **kwargs: Any,
    ) -> PromptResponse:
        del kwargs
        client = self._require_client()
        session = self._sessions.get(session_id) or self._adopt_session(session_id)
        session.touch()
        self._save_sessions()
        run = self._register_prompt_run(session_id)
        return await self._execute_prompt(prompt, session, client, run)

    async def ext_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, object]:
        if method != SESSION_STEERING_METHOD:
            raise RequestError.method_not_found(f"_{method}")

        try:
            session_id, prompt = self._parse_steering_params(params)
            return await self._execute_or_queue_steering(session_id, prompt)
        except RequestError:
            raise
        except Exception:
            logger.exception("Steering request failed")
            return {"outcome": "failed"}

    def _parse_steering_params(
        self, params: Mapping[str, object]
    ) -> tuple[str, list[ACPPromptBlock]]:
        session_id = params.get("sessionId")
        raw_prompt = params.get("prompt")
        if not isinstance(session_id, str) or not session_id:
            raise RequestError.invalid_params({"field": "sessionId"})
        if not isinstance(raw_prompt, list) or not raw_prompt:
            raise RequestError.invalid_params({"field": "prompt"})
        try:
            prompt = _PROMPT_ADAPTER.validate_python(raw_prompt)
        except ValidationError as error:
            raise RequestError.invalid_params(
                {"field": "prompt", "details": error.errors(include_url=False)}
            ) from error
        return session_id, prompt

    async def _execute_or_queue_steering(
        self, session_id: str, prompt: list[ACPPromptBlock]
    ) -> dict[str, object]:
        lock = self._steering_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise RequestError.invalid_params({"sessionId": session_id})
            if session_id in self._closing_sessions:
                raise RequestError.invalid_request({"sessionId": session_id})

            run = self._current_prompt_run(session_id)
            if run is not None and not run.started.is_set():
                await self._wait_for_prompt_start_or_completion(run)

            if run is not None and run.started.is_set() and not run.completed.is_set():
                inbound = self._build_inbound(prompt, session)
                receipt = await self._steering_inbox.enqueue_with_receipt(
                    inbound,
                    {
                        "session_id": _bub_session_id(
                            self.settings.channel_name, session_id
                        )
                    },
                )
                await self._wait_for_delivery_or_completion(receipt.delivered, run)
                if receipt.delivered.done():
                    return {"outcome": "injected"}
                pending = await self._steering_inbox.claim_pending(receipt)
                if pending is None:
                    return {"outcome": "injected"}

            await self._start_steering_turn(prompt, session)
            return {"outcome": "startedNewTurn"}

    async def _start_steering_turn(
        self, prompt: list[ACPPromptBlock], session: ACPSession
    ) -> None:
        if (
            session.session_id in self._closing_sessions
            or session.session_id not in self._sessions
        ):
            raise RequestError.invalid_request({"sessionId": session.session_id})

        session.touch()
        self._save_sessions()
        client = self._require_client()
        run = self._register_prompt_run(session.session_id, background=True)
        task = asyncio.create_task(
            self._execute_prompt(prompt, session, client, run),
            name=f"acp-steering-{session.session_id}",
        )
        run.task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_prompt_done)

        await self._wait_for_prompt_start_or_completion(run)
        if run.started.is_set():
            return
        await task
        raise RequestError.invalid_request(
            {"sessionId": session.session_id, "reason": "turn did not start"}
        )

    def _on_background_prompt_done(self, task: asyncio.Task[PromptResponse]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Steering-started prompt failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _execute_prompt(
        self,
        prompt: list[ACPPromptBlock],
        session: ACPSession,
        client: Client,
        run: ACPPromptRun,
    ) -> PromptResponse:
        try:
            inbound = self._build_inbound(prompt, session)
            if self.settings.send_user_message_updates:
                await self._send_user_message_updates(prompt, session.session_id)
            await self._process_inbound_with_streaming(
                inbound, session, client, run=run
            )
            return PromptResponse(stop_reason="end_turn")
        finally:
            self._complete_prompt_run(run)

    def _build_inbound(
        self, prompt: list[ACPPromptBlock], session: ACPSession
    ) -> ChannelMessage:
        content, media = _prompt_to_bub_content(prompt)
        context: dict[str, str] = {}
        if model := session.runtime.get("model"):
            context["_runtime_model"] = model
        if reasoning_effort := session.runtime.get(REASONING_EFFORT_CONFIG_ID):
            context["_runtime_reasoning_effort"] = reasoning_effort
        context["_runtime_workspace"] = str(session.cwd)
        return ChannelMessage(
            session_id=_bub_session_id(self.settings.channel_name, session.session_id),
            channel=self.settings.channel_name,
            chat_id=session.session_id,
            content=content,
            is_active=True,
            kind="normal",
            media=media,
            context=context,
        )

    def _register_prompt_run(
        self, session_id: str, *, background: bool = False
    ) -> ACPPromptRun:
        run = ACPPromptRun(session_id=session_id, background=background)
        self._prompt_runs.setdefault(session_id, deque()).append(run)
        return run

    def _complete_prompt_run(self, run: ACPPromptRun) -> None:
        run.completed.set()
        runs = self._prompt_runs.get(run.session_id)
        if runs is None:
            return
        with contextlib.suppress(ValueError):
            runs.remove(run)
        if not runs:
            self._prompt_runs.pop(run.session_id, None)

    def _current_prompt_run(self, session_id: str) -> ACPPromptRun | None:
        runs = self._prompt_runs.get(session_id)
        if not runs:
            return None
        for run in runs:
            if run.started.is_set() and not run.completed.is_set():
                return run
        return next((run for run in runs if not run.completed.is_set()), None)

    async def _wait_for_prompt_start_or_completion(self, run: ACPPromptRun) -> None:
        started = asyncio.create_task(run.started.wait())
        completed = asyncio.create_task(run.completed.wait())
        try:
            await asyncio.wait(
                {started, completed}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in (started, completed):
                if not waiter.done():
                    waiter.cancel()

    async def _wait_for_delivery_or_completion(
        self, delivered: asyncio.Future[None], run: ACPPromptRun
    ) -> None:
        completed = asyncio.create_task(run.completed.wait())
        try:
            await asyncio.wait(
                {delivered, completed}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not completed.done():
                completed.cancel()

    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("ACP client is not connected")
        return self._client

    def _require_stream_router(self) -> ACPStreamRouter:
        if self._stream_router is None:
            raise RuntimeError("ACP stream router is not connected")
        return self._stream_router

    def _adopt_session(self, session_id: str) -> ACPSession:
        session = ACPSession(session_id=session_id, cwd=self.framework.workspace)
        session.touch()
        self._sessions[session_id] = session
        self._save_sessions()
        return session

    def _load_or_adopt_session(
        self,
        *,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None,
    ) -> ACPSession:
        session = self._sessions.get(session_id)
        if session is None:
            session = ACPSession(
                session_id=session_id,
                cwd=Path(cwd).expanduser().resolve(),
                additional_directories=list(additional_directories or []),
            )
            self._sessions[session_id] = session
        else:
            session.cwd = Path(cwd).expanduser().resolve()
            session.additional_directories = list(additional_directories or [])
        session.touch()
        self._save_sessions()
        return session

    def _load_sessions(self) -> dict[str, ACPSession]:
        try:
            raw = json.loads(self._session_store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, list):
            return {}

        sessions: dict[str, ACPSession] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            session = ACPSession.from_json(item)
            if session is not None:
                sessions[session.session_id] = session
        return sessions

    def _save_sessions(self) -> None:
        self._session_store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [session.to_json() for session in self._sessions.values()]
        temp_path = self._session_store_path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        temp_path.replace(self._session_store_path)

    async def _attach_session_history(self, session: ACPSession) -> None:
        router = self._require_stream_router()
        inbound = ChannelMessage(
            session_id=_bub_session_id(self.settings.channel_name, session.session_id),
            channel=self.settings.channel_name,
            chat_id=session.session_id,
            content="",
            is_active=False,
            kind="normal",
        )
        try:
            async for _ in router.wrap_stream(
                inbound, self._session_history_stream(session)
            ):
                pass
        finally:
            router.pop_stream_state(session.session_id)

    async def _session_history_stream(
        self, session: ACPSession
    ) -> AsyncIterator[StreamEvent]:
        entries = await self._load_tape_entries(session)
        pending_tool_indices: list[int] = []
        next_tool_index = 0

        for entry in entries:
            if entry.kind == "message":
                event = _message_entry_stream_event(entry)
                if event is not None:
                    yield event
            elif entry.kind == "tool_call":
                calls = _list_payload(entry.payload.get("calls"))
                pending_tool_indices = []
                for call in calls:
                    tool_index = next_tool_index
                    next_tool_index += 1
                    pending_tool_indices.append(tool_index)
                    yield StreamEvent("tool_call", {"index": tool_index, "call": call})
            elif entry.kind == "tool_result":
                results = _list_payload(entry.payload.get("results"))
                for index, result in enumerate(results):
                    if index < len(pending_tool_indices):
                        tool_index = pending_tool_indices[index]
                    else:
                        tool_index = next_tool_index
                        next_tool_index += 1
                    yield StreamEvent(
                        "tool_result", {"index": tool_index, "result": result}
                    )
                pending_tool_indices = []
            elif entry.kind == "error":
                yield StreamEvent(
                    "error",
                    {
                        "message": _stringify(
                            entry.payload.get("message") or entry.payload
                        )
                    },
                )

    async def _load_tape_entries(self, session: ACPSession) -> list[TapeEntry]:
        tape_name = _session_tape_name(
            _bub_session_id(self.settings.channel_name, session.session_id),
            session.cwd,
        )
        store = _framework_tape_store(self.framework)
        if store is not None:
            query = TapeQuery(tape_name, store)
            with contextlib.suppress(Exception):
                result = store.fetch_all(query)
                if inspect.isawaitable(result):
                    result = await result
                return list(cast(Iterable[TapeEntry], result))
        return _load_tape_entries_from_file(
            bub.home.expanduser() / "tapes" / f"{tape_name}.jsonl"
        )

    async def _session_config_options(
        self, session: ACPSession
    ) -> list[SessionConfigOptionSelect]:
        model_options = await self.framework.get_model_options(
            session_id=_bub_session_id(self.settings.channel_name, session.session_id),
            workspace=session.cwd,
        )
        acp_options = _model_options_to_acp_config_options(model_options, session)
        acp_options.append(_reasoning_effort_config_option(session))
        return acp_options

    async def _set_session_config_option(
        self,
        session: ACPSession,
        config_id: str,
        value: str | bool,
    ) -> list[SessionConfigOptionSelect]:
        if not isinstance(value, str):
            raise ValueError(
                f"invalid value for ACP config option {config_id}: {value}"
            )
        config_options = await self._session_config_options(session)
        selected_option = next(
            (option for option in config_options if option.id == config_id),
            None,
        )
        if selected_option is None:
            raise ValueError(f"unknown ACP config option: {config_id}")
        allowed_values = {option.value for option in selected_option.options}
        if value not in allowed_values:
            raise ValueError(
                f"invalid value for ACP config option {config_id}: {value}"
            )
        session.runtime[config_id] = value
        selected_option.current_value = value
        return config_options

    async def _send_user_message_updates(
        self, prompt: list[ACPPromptBlock], session_id: str
    ) -> None:
        client = self._require_client()
        for block in prompt:
            if _block_type(block) == "text":
                await client.session_update(session_id, update_user_message(block))

    async def _process_inbound_with_streaming(
        self,
        inbound: ChannelMessage,
        session: ACPSession,
        client: Client,
        *,
        run: ACPPromptRun,
    ) -> TurnResult:
        async with self._prompt_lock:
            if run.background and (
                session.session_id in self._closing_sessions
                or session.session_id not in self._sessions
            ):
                raise RequestError.invalid_request({"sessionId": session.session_id})
            run.started.set()
            router = self._require_stream_router()
            try:
                result = await self.framework.process_inbound(
                    inbound, stream_output=True
                )
            except BaseException:
                router.pop_stream_state(session.session_id)
                raise
            stream_state = router.pop_stream_state(session.session_id)
            if result.model_output and not (
                stream_state is not None and stream_state.sent_text
            ):
                await client.session_update(
                    session.session_id, update_agent_message_text(result.model_output)
                )
            return result


async def run_acp_agent(
    framework: BubFramework, *, use_unstable_protocol: bool = True
) -> None:
    agent = BubACPAgent(framework)
    with replace_builtin_tools(agent.client_tools):
        async with framework.running():
            get_steering_inbox = getattr(framework, "get_steering_inbox", None)
            if callable(get_steering_inbox):
                steering_inbox = get_steering_inbox()
                if isinstance(steering_inbox, ACPSteeringInbox):
                    agent.set_steering_inbox(steering_inbox)
            await run_agent(agent, use_unstable_protocol=use_unstable_protocol)


def _message_chat_id(message: Envelope) -> str:
    chat_id = field_of(message, "chat_id")
    if chat_id is None or not str(chat_id).strip():
        raise RuntimeError("Bub message does not contain a chat id")
    return str(chat_id)


def _bub_session_id(channel: str, chat_id: str) -> str:
    return f"{channel}:{chat_id}"


def _prompt_to_bub_content(prompt: list[ACPPromptBlock]) -> tuple[str, list[MediaItem]]:
    parts: list[str] = []
    media: list[MediaItem] = []
    for block in prompt:
        block_type = _block_type(block)
        if block_type == "text":
            parts.append(str(_block_value(block, "text", "")))
        elif block_type == "image":
            media.append(_media_item(block, media_type="image"))
            parts.append(_attachment_label(block, "image"))
        elif block_type == "audio":
            media.append(_media_item(block, media_type="audio"))
            parts.append(_attachment_label(block, "audio"))
        elif block_type == "resource_link":
            name = _block_value(block, "name", "resource")
            uri = _block_value(block, "uri", "")
            parts.append(f"[resource: {name}] {uri}".strip())
        elif block_type == "resource":
            parts.append(_embedded_resource_text(block))
        else:
            parts.append(f"[unsupported ACP content: {block_type}]")
    content = "\n".join(part for part in parts if part).strip()
    return content or "[ACP prompt attachment]", media


def _media_item(block: ACPPromptBlock, *, media_type: MediaType) -> MediaItem:
    data = str(_block_value(block, "data", ""))
    mime_type = str(_block_value(block, "mime_type", "application/octet-stream"))

    async def fetch_data() -> bytes:
        return base64.b64decode(data)

    return MediaItem(type=media_type, mime_type=mime_type, data_fetcher=fetch_data)


def _embedded_resource_text(block: ACPPromptBlock) -> str:
    resource = _block_value(block, "resource", None)
    if resource is None:
        return "[resource]"
    text = _block_value(resource, "text", None)
    if text is not None:
        return str(text)
    uri = _block_value(resource, "uri", "")
    return f"[resource: {uri}]".strip()


def _attachment_label(block: ACPPromptBlock, kind: str) -> str:
    uri = _block_value(block, "uri", None)
    return f"[{kind}: {uri}]" if uri else f"[{kind}]"


def _block_type(block: object) -> str:
    return str(_block_value(block, "type", ""))


def _block_value(block: object, name: str, default: object = None) -> object:
    if isinstance(block, Mapping):
        return block.get(name, default)
    return getattr(block, name, default)


def _tool_call_id(index: int, call: object) -> str:
    candidate = _block_value(call, "id", None) or _block_value(
        call, "tool_call_id", None
    )
    return str(candidate or f"tool-{index}")


def _tool_name(call: object) -> str:
    name = _block_value(call, "name", None)
    if name is None:
        function = _block_value(call, "function", None)
        name = _block_value(function, "name", None)
    return str(name or "tool")


def _tool_raw_input(call: object) -> object:
    function = _block_value(call, "function", None)
    arguments = _block_value(function, "arguments", None)
    if arguments is None:
        arguments = _block_value(call, "arguments", None)
    if isinstance(arguments, str):
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(arguments)
    if arguments is not None:
        return arguments
    return call


def _tool_command(call: object) -> str | None:
    raw_input = _tool_raw_input(call)
    command = _block_value(raw_input, "cmd", None)
    return command if isinstance(command, str) and command else None


def _tool_title(call: object) -> str:
    name = _tool_name(call)
    if name == "bash" and (command := _tool_command(call)):
        return command
    return name


def _tool_kind(name: str) -> ToolKind:
    lower_name = name.lower()
    if any(token in lower_name for token in ("read", "cat", "view")):
        return "read"
    if any(token in lower_name for token in ("write", "edit", "patch")):
        return "edit"
    if any(token in lower_name for token in ("delete", "remove", "rm")):
        return "delete"
    if any(token in lower_name for token in ("search", "grep", "rg")):
        return "search"
    if any(token in lower_name for token in ("bash", "shell", "exec", "run")):
        return "execute"
    return "other"


def _int_value(value: object, *, default: int) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return int(value)
    return default


def _event_usage(event: StreamEvent) -> object:
    if event.kind != "usage":
        return None
    nested_usage = event.data.get("usage")
    return nested_usage if isinstance(nested_usage, Mapping) else event.data


def _usage_total_tokens(usage: Mapping[str, object] | None) -> int | None:
    if usage is None:
        return None

    total = _non_negative_int(usage.get("total_tokens"))
    if total is not None:
        return total

    input_tokens = _first_token_count(usage, "input_tokens", "prompt_tokens")
    output_tokens = _first_token_count(usage, "output_tokens", "completion_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return (input_tokens or 0) + (output_tokens or 0)


def _usage_context_window_size(usage: Mapping[str, object] | None) -> int | None:
    if usage is None:
        return None
    return _first_token_count(
        usage,
        "context_window_size",
        "context_window",
        "max_context_tokens",
    )


def _first_token_count(usage: Mapping[str, object], *keys: str) -> int | None:
    for key in keys:
        value = _non_negative_int(usage.get(key))
        if value is not None:
            return value
    return None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    with contextlib.suppress(TypeError, ValueError):
        result = int(value)
        if result >= 0:
            return result
    return None


def _framework_tape_store(framework: BubFramework) -> object | None:
    get_tape_store = getattr(framework, "get_tape_store", None)
    if get_tape_store is None:
        return None
    store = get_tape_store()
    return store if hasattr(store, "fetch_all") else None


def _model_options_to_acp_config_options(
    model_options: ModelOptions, session: ACPSession
) -> list[SessionConfigOptionSelect]:
    choices = model_options.models
    if not choices:
        return []

    choice_ids = {choice.id for choice in choices}
    current_value = session.runtime.get("model")
    if current_value not in choice_ids:
        current_value = model_options.current_model
    if current_value not in choice_ids:
        current_value = choices[0].id
    return [
        SessionConfigOptionSelect(
            type="select",
            id="model",
            name="Model",
            current_value=current_value,
            options=[_model_choice_to_acp_option(choice) for choice in choices],
            category="model",
        )
    ]


def _model_choice_to_acp_option(choice: ModelChoice) -> SessionConfigSelectOption:
    return SessionConfigSelectOption(
        value=choice.id,
        name=choice.name or choice.id,
        description=choice.description,
        field_meta=dict(choice.meta) if choice.meta is not None else None,
    )


def _reasoning_effort_config_option(
    session: ACPSession,
) -> SessionConfigOptionSelect:
    allowed_values = {value for value, _ in REASONING_EFFORT_OPTIONS}
    current_value = session.runtime.get(REASONING_EFFORT_CONFIG_ID, "auto")
    if current_value not in allowed_values:
        current_value = "auto"
    return SessionConfigOptionSelect(
        type="select",
        id=REASONING_EFFORT_CONFIG_ID,
        name="Reasoning effort",
        description="How much reasoning effort the model should use",
        category="thought_level",
        current_value=current_value,
        options=[
            SessionConfigSelectOption(value=value, name=name)
            for value, name in REASONING_EFFORT_OPTIONS
        ],
    )


def _session_tape_name(session_id: str, workspace: Path) -> str:
    workspace_hash = hashlib.md5(
        str(workspace.resolve()).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16]
    session_hash = hashlib.md5(
        session_id.encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16]
    return f"{workspace_hash}__{session_hash}"


def _load_tape_entries_from_file(path: Path) -> list[TapeEntry]:
    entries: list[TapeEntry] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                entry = _tape_entry_from_json_line(raw_line)
                if entry is not None:
                    entries.append(entry)
    except OSError:
        return []
    return entries


def _tape_entry_from_json_line(line: str) -> TapeEntry | None:
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    entry_id = payload.get("id")
    kind = payload.get("kind")
    entry_payload = payload.get("payload")
    meta = payload.get("meta")
    date = payload.get("date")
    if (
        not isinstance(entry_id, int)
        or not isinstance(kind, str)
        or not isinstance(entry_payload, dict)
    ):
        return None
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(date, str):
        date = datetime.fromtimestamp(0.0, tz=UTC).isoformat()
    return TapeEntry(entry_id, kind, dict(entry_payload), dict(meta), date)


def _message_entry_stream_event(entry: TapeEntry) -> StreamEvent | None:
    role = entry.payload.get("role")
    content = _message_content(entry.payload.get("content"))
    if not content:
        return None

    if role == "user":
        user_content = _clean_user_tape_content(content)
        if not user_content:
            return None
        return StreamEvent("user_text", {"delta": user_content})
    if role == "assistant":
        return StreamEvent("text", {"delta": content})
    return None


def _message_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""

    parts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def _clean_user_tape_content(content: str) -> str:
    cleaned = _BUB_PROMPT_CONTEXT.sub("", content, count=1).strip()
    if cleaned.startswith(_CONTINUATION_PROMPT_PREFIX):
        return ""
    return cleaned


def _list_payload(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return repr(value)
