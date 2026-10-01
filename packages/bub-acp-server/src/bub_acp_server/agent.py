from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import bub
from acp import run_agent
from acp.interfaces import Client
from acp.exceptions import RequestError
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    AudioContentBlock,
    ClientCapabilities,
    CloseSessionResponse,
    ContentToolCallContent,
    DeleteSessionResponse,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    Implementation,
    InitializeResponse,
    ListSessionsResponse,
    LoadSessionResponse,
    McpCapabilities,
    NewSessionResponse,
    PromptResponse,
    ResourceContentBlock,
    ResumeSessionResponse,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionDeleteCapabilities,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionInfo,
    SessionInfoUpdate,
    SessionListCapabilities,
    SessionResumeCapabilities,
    SetSessionConfigOptionResponse,
    TextContentBlock,
    TerminalToolCallContent,
    ToolCallProgress,
    ToolCallStart,
    ToolKind,
    UsageUpdate,
    UserMessageChunk,
)
from bub.channels.message import ChannelMessage, MediaItem, MediaType
from bub.envelope import Envelope, content_of, field_of
from bub.model_selection import ModelChoice, ModelOptions
from bub.sidecars import sidecar_tape_name
from bub.streaming import StreamEvent
from bub.tools import ToolContext
from bub.tape import (
    AsyncTapeStoreAdapter,
    Tape,
    TapeContext,
    TapeEntry,
    is_async_tape_store,
)
from pydantic import TypeAdapter, ValidationError

from bub_acp_server.client_tools import ACPClientToolRuntime, build_client_tools
from bub_acp_server.config import ACPServerSettings
from bub_acp_server.mcp import ACPMcpServer, connect_session_mcp, server_configs
from bub_acp_server.subagents import (
    LODY_SUBAGENT_EVENTS_CAPABILITY,
    SubagentEventEmitter,
    client_supports_subagent_events,
    json_safe,
)
from bub_mcp.plugin import MCPChannel
from bub_acp_server.steering import ACPSteeringInbox

if TYPE_CHECKING:
    from bub.builtin.agent import Agent
    from bub.builtin.tools import SubAgentInput, SubAgentResult
    from bub.framework import BubFramework

type ACPPromptBlock = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)
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
    ("max", "Max"),
    ("ultra", "Ultra"),
)

_PROMPT_ADAPTER = TypeAdapter(list[ACPPromptBlock])

logger = logging.getLogger(__name__)

LODY_SESSION_STEERING_METHOD = "lody/session/steer"
LODY_SESSION_STEERING_APPLIED_METHOD = "lody/session/steer_applied"
LEGACY_SESSION_STEERING_METHOD = "session/steering"
LEGACY_SESSION_STEERING_APPLIED_METHOD = "session/steering_applied"

_STEERING_APPLIED_METHODS = {
    LODY_SESSION_STEERING_METHOD: LODY_SESSION_STEERING_APPLIED_METHOD,
    LEGACY_SESSION_STEERING_METHOD: LEGACY_SESSION_STEERING_APPLIED_METHOD,
}

_LODY_STEERING_CAPABILITY = {
    "version": 1,
    "transport": "request",
    "upstreamTurn": "same",
    "configPolicy": "active",
}

_LODY_EXTENSION_CAPABILITIES: dict[str, object] = {
    "steering": _LODY_STEERING_CAPABILITY,
    "subagentEvents": dict(LODY_SUBAGENT_EVENTS_CAPABILITY),
    "sessionTitle": {"version": 1},
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
class ACPToolCallState:
    tool_id: str
    name: str = "tool"
    command: str | None = None
    terminal_id: str | None = None


@dataclass(slots=True)
class ACPStreamState:
    pending_tools: list[ACPToolCallState] = field(default_factory=list)
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
        if event.kind in ("text", "reasoning", "user_text"):
            delta = str(event.data.get("delta", ""))
            if delta:
                if event.kind == "text":
                    state.sent_text = True
                update = {
                    "text": AgentMessageChunk,
                    "reasoning": AgentThoughtChunk,
                    "user_text": UserMessageChunk,
                }[event.kind](content=TextContentBlock(text=delta))
                await self._client.session_update(session_id, update)
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
        await self._client.session_update(
            session_id, AgentMessageChunk(content=TextContentBlock(text=text))
        )

    async def _send_tool_calls(self, session_id: str, data: StreamPayload) -> None:
        state = self._stream_states[session_id]
        state.pending_tools = []
        for call in _list_payload(data.get("tool_calls")):
            tool = ACPToolCallState(
                tool_id=_tool_call_id(state.next_tool_index, call),
                name=_tool_name(call),
            )
            state.next_tool_index += 1
            state.pending_tools.append(tool)
            raw_input = _tool_raw_input(call)
            title = tool.name
            if tool.name == "bash":
                command = _block_value(raw_input, "command")
                if isinstance(command, str) and command:
                    tool.command = command
                    title = command
            is_context_compaction = tool.name == "tape.handoff"
            update = ToolCallStart(
                tool_call_id=tool.tool_id,
                title="Context compacting" if is_context_compaction else title,
                kind="other" if is_context_compaction else _tool_kind(tool.name),
                status="in_progress",
                raw_input=raw_input,
            )
            if is_context_compaction:
                update.field_meta = {"contextCompaction": True}
            await self._client.session_update(session_id, update)

    async def attach_terminal(
        self, session_id: str, command: str, terminal_id: str
    ) -> None:
        state = self._stream_states.get(session_id)
        if state is None:
            return
        pending = [
            tool
            for tool in state.pending_tools
            if tool.name == "bash" and tool.terminal_id is None
        ]
        if not pending:
            return
        tool = next((tool for tool in pending if tool.command == command), pending[0])
        tool.terminal_id = terminal_id
        await self._client.session_update(
            session_id,
            ToolCallProgress(
                tool_call_id=tool.tool_id,
                title=tool.command,
                status="in_progress",
                content=[
                    TerminalToolCallContent(terminal_id=terminal_id),
                ],
            ),
        )

    async def _send_tool_results(self, session_id: str, data: StreamPayload) -> None:
        state = self._stream_states[session_id]
        for position, result in enumerate(_list_payload(data.get("tool_results"))):
            if position < len(state.pending_tools):
                tool = state.pending_tools[position]
            else:
                tool = ACPToolCallState(tool_id=f"tool-{state.next_tool_index}")
                state.next_tool_index += 1
            content = None
            is_context_compaction = tool.name == "tape.handoff"
            # The client may discard terminal output on release before this event.
            # Persist a text snapshot even when a live terminal was attached.
            if not is_context_compaction:
                output = _stringify(result)
                content = [
                    ContentToolCallContent(content=TextContentBlock(text=output))
                ]
            update = ToolCallProgress(
                tool_call_id=tool.tool_id,
                title="Context compacted" if is_context_compaction else tool.command,
                status="completed",
                raw_output=result,
                content=content,
            )
            if is_context_compaction:
                update.field_meta = {"contextCompaction": True}
            await self._client.session_update(session_id, update)
        state.pending_tools = []


active_stream_router: ContextVar[ACPStreamRouter | None] = ContextVar(
    "acp_stream_router", default=None
)


@dataclass
class ACPInboundMessage(ChannelMessage):
    _runtime_agent: Agent | None = None
    _mcp_channel: MCPChannel | None = None


class BubACPAgent:
    def __init__(
        self,
        framework: BubFramework,
        *,
        client_tools: ACPClientToolRuntime | None = None,
        steering_inbox: ACPSteeringInbox | None = None,
        prompt_lock: asyncio.Lock | None = None,
        sessions: dict[str, ACPSession] | None = None,
        mcp_channels: dict[str, MCPChannel] | None = None,
        prompt_runs: dict[str, deque[ACPPromptRun]] | None = None,
        closing_sessions: set[str] | None = None,
        bind_router: bool = True,
        use_unstable_protocol: bool = True,
    ) -> None:
        self.framework = framework
        self.settings = bub.ensure_config(ACPServerSettings)
        self.client_tools = client_tools or ACPClientToolRuntime()
        self._runtime_agents: dict[str, Agent] = {}
        self._mcp_channels = mcp_channels if mcp_channels is not None else {}
        self._mcp_connect_tasks: set[asyncio.Task[MCPChannel | None]] = set()
        self._closed = False
        self._client: Client | None = None
        self._stream_router: ACPStreamRouter | None = None
        self._bind_router = bind_router
        self._use_unstable_protocol = use_unstable_protocol
        self._session_store_path = bub.home.expanduser() / "acp-sessions.json"
        self._sessions = self._load_sessions() if sessions is None else sessions
        self._prompt_lock = prompt_lock if prompt_lock is not None else asyncio.Lock()
        self._steering_inbox = steering_inbox or ACPSteeringInbox()
        self._prompt_runs = prompt_runs if prompt_runs is not None else {}
        self._steering_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[PromptResponse]] = set()
        self._subagent_events_enabled = False
        self._lody_client = False
        self._title_tasks: dict[str, asyncio.Task[None]] = {}
        self._title_sessions: set[str] = set()
        self._closing_sessions = (
            closing_sessions if closing_sessions is not None else set()
        )

    def set_steering_inbox(self, steering_inbox: ACPSteeringInbox) -> None:
        self._steering_inbox = steering_inbox

    def on_connect(self, conn: Client) -> None:
        self._client = conn
        self.client_tools.connect(conn)
        self._stream_router = ACPStreamRouter(
            conn, context_window_size=self.settings.context_window_size
        )
        self.client_tools.set_terminal_observer(self._stream_router.attach_terminal)

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
        del kwargs
        self.client_tools.set_capabilities(client_capabilities)
        # Subagent events are opt-in on both sides: publishing needs the client to
        # advertise the same version, whoever the client is.
        self._subagent_events_enabled = client_supports_subagent_events(
            client_capabilities
        )
        # `_meta.lody` is the Lody extension surface, so it is advertised only to
        # clients that identify themselves as Lody. Other clients see nothing
        # Lody-namespaced and never receive Lody-specific behavior such as
        # automatic session titles.
        self._lody_client = is_lody_client(client_info)
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_info=Implementation(name="bub", title="Bub", version=bub_version),
            field_meta={"steering": {"supported": True}},
            agent_capabilities=AgentCapabilities(
                load_session=True,
                mcp_capabilities=McpCapabilities(http=True, sse=True),
                field_meta=(
                    {"lody": dict(_LODY_EXTENSION_CAPABILITIES)}
                    if self._lody_client
                    else None
                ),
                session_capabilities=SessionCapabilities(
                    delete=SessionDeleteCapabilities(),
                    close=SessionCloseCapabilities()
                    if self._use_unstable_protocol
                    else None,
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities()
                    if self._use_unstable_protocol
                    else None,
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
        del kwargs
        session = self._load_or_adopt_session(
            session_id=uuid4().hex,
            cwd=cwd,
            additional_directories=additional_directories,
        )
        try:
            await self._replace_session_mcp(session, mcp_servers)
        except BaseException:
            self._sessions.pop(session.session_id, None)
            self._save_sessions()
            raise
        return NewSessionResponse(
            session_id=session.session_id,
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
        del kwargs
        async with self._prompt_lock:
            session = self._load_or_adopt_session(
                session_id=session_id,
                cwd=cwd,
                additional_directories=additional_directories,
            )
            await self._replace_session_mcp(session, mcp_servers)
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
        del kwargs
        async with self._prompt_lock:
            session = self._load_or_adopt_session(
                session_id=session_id,
                cwd=cwd,
                additional_directories=additional_directories,
            )
            await self._replace_session_mcp(session, mcp_servers)
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
        sessions_on_disk = self._load_sessions()
        self._sessions.clear()
        self._sessions.update(sessions_on_disk)
        sessions = sorted(
            self._sessions.values(),
            key=lambda item: item.updated_at or "",
            reverse=True,
        )
        return ListSessionsResponse(sessions=[session.info() for session in sessions])

    async def delete_session(
        self, session_id: str, **kwargs: Any
    ) -> DeleteSessionResponse:
        """Remove the session, its tape, and its active runtime resources."""
        del kwargs
        await self._end_session(session_id, delete_tape=True)
        return DeleteSessionResponse()

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> CloseSessionResponse | None:
        del kwargs
        await self._end_session(session_id, delete_tape=False)
        return CloseSessionResponse()

    async def _end_session(self, session_id: str, *, delete_tape: bool) -> None:
        if session_id in self._closing_sessions:
            raise RequestError.invalid_request({"sessionId": session_id})
        session = self._sessions.get(session_id) or self._load_sessions().get(
            session_id
        )
        self._closing_sessions.add(session_id)
        try:
            self._sessions.pop(session_id, None)
            self._save_sessions()
            runs = list(self._prompt_runs.get(session_id, ()))
            tasks = [run.task for run in runs if run.task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for run in runs:
                # A task cancelled before its first step never enters its finally block.
                self._complete_prompt_run(run)
            channel = self._mcp_channels.pop(session_id, None)
            if channel is not None:
                await channel.stop()
            self._runtime_agents.pop(session_id, None)
            if delete_tape and session is not None:
                tape = self._session_tape(session)
                if tape is not None:
                    sidecars = getattr(
                        self.framework, "get_tape_sidecars", lambda: ()
                    )()
                    for sidecar in sidecars:
                        await tape.store.reset(
                            sidecar_tape_name(tape.name, sidecar.name)
                        )
                    # Tape.reset() creates a new session/start entry; deletion must not.
                    await tape.store.reset(tape.name)
        except BaseException:
            if delete_tape and session is not None:
                # Keep the workspace metadata so a failed deletion can be retried.
                self._sessions[session_id] = session
                self._save_sessions()
            raise
        finally:
            self._steering_locks.pop(session_id, None)
            self._title_sessions.discard(session_id)
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
        session = self._sessions.get(session_id) or self._adopt_session(session_id)
        session.touch()
        self._save_sessions()
        run = self._register_prompt_run(session_id)
        run.task = asyncio.current_task()
        return await self._execute_prompt(prompt, session, run)

    async def ext_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, object]:
        applied_method = _STEERING_APPLIED_METHODS.get(method)
        if applied_method is None:
            raise RequestError.method_not_found(f"_{method}")

        try:
            session_id, prompt, steer_id = self._parse_steering_params(params)
            outcome = await self._execute_or_queue_steering(session_id, prompt)
            if steer_id is not None:
                await self._require_client().ext_notification(
                    applied_method,
                    {"sessionId": session_id, "steerId": steer_id},
                )
                return {"outcome": "injected"}
            return outcome
        except RequestError:
            raise
        except Exception:
            logger.exception("Steering request failed")
            return {"outcome": "failed"}

    def _parse_steering_params(
        self, params: Mapping[str, object]
    ) -> tuple[str, list[ACPPromptBlock], str | None]:
        session_id = params.get("sessionId")
        raw_prompt = params.get("prompt")
        raw_steer_id = params.get("steerId")
        if not isinstance(session_id, str) or not session_id:
            raise RequestError.invalid_params({"field": "sessionId"})
        if not isinstance(raw_prompt, list) or not raw_prompt:
            raise RequestError.invalid_params({"field": "prompt"})
        if raw_steer_id is not None and (
            not isinstance(raw_steer_id, str) or not raw_steer_id
        ):
            raise RequestError.invalid_params({"field": "steerId"})
        try:
            prompt = _PROMPT_ADAPTER.validate_python(raw_prompt)
        except ValidationError as error:
            raise RequestError.invalid_params(
                {"field": "prompt", "details": error.errors(include_url=False)}
            ) from error
        return session_id, prompt, raw_steer_id

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
        run = self._register_prompt_run(session.session_id)
        task = asyncio.create_task(
            self._execute_prompt(prompt, session, run),
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
        run: ACPPromptRun,
    ) -> PromptResponse:
        try:
            client = self._require_client()
            if self.settings.send_user_message_updates:
                await self._send_user_message_updates(prompt, session.session_id)
            async with self._prompt_lock:
                if run.task is not None and (
                    session.session_id in self._closing_sessions
                    or session.session_id not in self._sessions
                ):
                    raise RequestError.invalid_request(
                        {"sessionId": session.session_id}
                    )
                inbound = self._build_inbound(prompt, session)
                run.started.set()
                router = self._require_stream_router()
                token = active_stream_router.set(router)
                try:
                    # Gateway owns its router; its ACP channel routes this turn's stream.
                    if self._bind_router:
                        self.framework.bind_channel_router(router)
                    result = await self.framework.process_inbound(
                        inbound, stream_output=True
                    )
                finally:
                    active_stream_router.reset(token)
                    stream_state = router.pop_stream_state(session.session_id)
                if result.model_output and not (
                    stream_state is not None and stream_state.sent_text
                ):
                    await client.session_update(
                        session.session_id,
                        AgentMessageChunk(
                            content=TextContentBlock(text=result.model_output)
                        ),
                    )
            return PromptResponse(stop_reason="end_turn")
        finally:
            self._complete_prompt_run(run)
            self._schedule_session_title(prompt, session)

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
        inbound = ACPInboundMessage(
            session_id=_bub_session_id(self.settings.channel_name, session.session_id),
            channel=self.settings.channel_name,
            chat_id=session.session_id,
            content=content,
            is_active=True,
            kind="normal",
            media=media,
            context=context,
        )
        runtime_agent = self._runtime_agents.get(session.session_id)
        if runtime_agent is None:
            from bub.builtin.agent import Agent

            runtime_agent = Agent(self.framework)
            runtime_agent.tools.update(build_client_tools(self.client_tools))
            if self._subagent_events_enabled:
                self._install_streaming_subagent(runtime_agent, session)
            self._runtime_agents[session.session_id] = runtime_agent
        # Bub's builtin load_state uses this instance for recovery and execution.
        inbound._runtime_agent = runtime_agent
        inbound._mcp_channel = self._mcp_channels.get(session.session_id)
        return inbound

    def _install_streaming_subagent(
        self, runtime_agent: Agent, session: ACPSession
    ) -> None:
        """Replace this session's `subagent` tool with a streaming variant.

        The replacement keeps the original tool's name, description, parameters,
        renderer, output schema and code-mode flags, so the model sees no
        difference. Only the handler changes: it republishes the child run
        through `_lody/subagents/event` while still returning Bub's result.
        """

        tool = runtime_agent.tools.get("subagent")
        if tool is None:
            return

        async def handler(*args: Any, **kwargs: Any) -> Any:
            from bub.builtin.tools import SubAgentInput

            context = kwargs.pop("context", None)
            param = SubAgentInput(*args, **kwargs)
            return await self._run_streaming_subagent(param, context, session)

        runtime_agent.tools["subagent"] = replace(tool, handler=handler)

    async def _run_streaming_subagent(
        self,
        param: SubAgentInput,
        context: ToolContext | None,
        session: ACPSession,
    ) -> SubAgentResult:
        from bub.builtin.tools import resolve_tool_names

        if context is None:
            raise RuntimeError("subagent tool requires a tool context")
        agent = context.state.get("_runtime_agent")
        if agent is None:
            raise RuntimeError("no runtime agent found in tool context")

        emitter = SubagentEventEmitter(self._require_client(), session.session_id)
        child_session = _subagent_session_id(
            param.session, str(context.state.get("session_id") or "")
        )
        child_state = {**context.state, "session_id": child_session}
        allowed_tools = resolve_tool_names(
            param.allowed_tools or None, exclude={"subagent"}, all_names=agent.known_tools
        )
        await emitter.started(
            description=_subagent_description(param.prompt), model_id=param.model
        )

        started_at = time.time()
        output: list[str] = []
        errors: list[str] = []
        pending: list[tuple[str, str]] = []
        next_index = 0
        turn_count = 0
        tool_call_count = 0
        last_tool_name: str | None = None
        reported_usage: object = None

        def elapsed_ms() -> int:
            return int((time.time() - started_at) * 1000)

        try:
            stream = await agent.run_stream(
                session_id=child_session,
                prompt=param.prompt,
                state=child_state,
                model=param.model,
                allowed_tools=allowed_tools,
                allowed_skills=param.allowed_skills,
            )
            async with contextlib.aclosing(stream):
                async for event in stream:
                    if event.kind in ("text", "reasoning"):
                        delta = str(event.data.get("delta", ""))
                        if not delta:
                            continue
                        if event.kind == "text":
                            output.append(delta)
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
                            tool_id = _tool_call_id(next_index, call)
                            name = _tool_name(call)
                            raw_input = _tool_raw_input(call)
                            next_index += 1
                            tool_call_count += 1
                            last_tool_name = name
                            title = _tool_title(name, raw_input)
                            pending.append((tool_id, title))
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
                        await emitter.progress(
                            lastToolName=last_tool_name,
                            turnCount=turn_count,
                            toolCallCount=tool_call_count,
                            durationMs=elapsed_ms(),
                        )
                    elif event.kind == "tool_result":
                        results = _list_payload(event.data.get("tool_results"))
                        for position, result in enumerate(results):
                            if position < len(pending):
                                tool_id, title = pending[position]
                            else:
                                tool_id, title = f"tool-{next_index}", "tool"
                                next_index += 1
                            await emitter.output(
                                {
                                    "sessionUpdate": "tool_call_update",
                                    "toolCallId": tool_id,
                                    "title": title,
                                    "status": "completed",
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
                        pending = []
                    elif event.kind == "error":
                        errors.append(
                            str(
                                event.data.get("message")
                                or event.data.get("error")
                                or "unknown error"
                            )
                        )
                    elif event.kind == "usage":
                        reported_usage = _event_usage(event)
                    elif event.kind == "final":
                        turn_count += 1
        except asyncio.CancelledError:
            await emitter.snapshot("cancelled", reason_code="cancelled")
            raise
        except Exception as error:
            await emitter.snapshot(
                "failed", reason_code="error", reason_message=str(error)
            )
            raise

        text = "".join(output)
        # Progress counters are display observations, never usage accounting input.
        await emitter.progress(
            lastToolName=last_tool_name,
            turnCount=turn_count,
            toolCallCount=tool_call_count,
            durationMs=elapsed_ms(),
            totalTokens=_subagent_total_tokens(
                getattr(stream, "usage", None) or reported_usage
            ),
        )
        await emitter.snapshot("completed", summary=_summary(text))
        return {"session_id": child_session, "output": text, "errors": errors}

    def _schedule_session_title(
        self, prompt: list[ACPPromptBlock], session: ACPSession
    ) -> None:
        """Start best-effort title generation for a session's first turn.

        Only Lody clients receive titles: the capability is a promise that the
        client can drop its own title generator for this session.
        """

        if not self._lody_client:
            return
        if session.session_id not in self._sessions:
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
                "Session title generation failed for {}: {}",
                session.session_id,
                error,
            )
            return

        title = _clean_title(generated)
        if not title or session.session_id not in self._sessions:
            return
        session.title = title
        self._save_sessions()
        client = self._client
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
                "Failed to push the session title for {}: {}",
                session.session_id,
                error,
            )

    async def _generate_session_title(self, session: ACPSession, request: str) -> str:
        """Ask the session's model for a short title using a throwaway tape."""

        agent = self._runtime_agents.get(session.session_id)
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

    async def _replace_session_mcp(
        self, session: ACPSession, servers: list[ACPMcpServer] | None
    ) -> None:
        if self._closed:
            raise RequestError.invalid_request({"reason": "Agent is shutting down"})
        configs = server_configs(servers, session.cwd)
        task = asyncio.create_task(connect_session_mcp(configs))
        self._mcp_connect_tasks.add(task)
        try:
            channel = await task
        finally:
            self._mcp_connect_tasks.discard(task)
        if session.session_id not in self._sessions:
            if channel is not None:
                await channel.stop()
            raise RequestError.invalid_request({"sessionId": session.session_id})
        previous = self._mcp_channels.pop(session.session_id, None)
        if channel is not None:
            self._mcp_channels[session.session_id] = channel
        if previous is not None:
            await previous.stop()

    async def shutdown(self) -> None:
        """Cancel pending turns and release session MCP resources."""
        self._closed = True
        tasks = (
            {
                run.task
                for runs in self._prompt_runs.values()
                for run in runs
                if run.task is not None and run.task is not asyncio.current_task()
            }
            | self._background_tasks
            | self._mcp_connect_tasks
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        channels = list(self._mcp_channels.values())
        self._mcp_channels.clear()
        await asyncio.gather(*(channel.stop() for channel in channels))
        self._runtime_agents.clear()

    def _register_prompt_run(self, session_id: str) -> ACPPromptRun:
        run = ACPPromptRun(session_id=session_id)
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
        if session_id in self._closing_sessions:
            raise RequestError.invalid_request({"sessionId": session_id})
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
        if session_id in self._closing_sessions:
            raise RequestError.invalid_request({"sessionId": session_id})
        workspace = Path(cwd).expanduser().resolve()
        session = self._sessions.get(session_id) or ACPSession(session_id, workspace)
        session.cwd = workspace
        session.additional_directories = list(additional_directories or [])
        self._sessions[session_id] = session
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
        for entry in entries:
            if entry.kind == "message":
                event = _message_entry_stream_event(entry)
                if event is not None:
                    yield event
            elif entry.kind == "tool_call":
                yield StreamEvent(
                    "tool_call", {"tool_calls": entry.payload.get("calls")}
                )
            elif entry.kind == "tool_result":
                yield StreamEvent(
                    "tool_result", {"tool_results": entry.payload.get("results")}
                )
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
        tape = self._session_tape(session)
        return await tape.search(tape.query()) if tape is not None else []

    def _session_tape(self, session: ACPSession) -> Tape | None:
        store = self.framework.get_tape_store()
        if store is None:
            return None
        return Tape(
            archive_path=bub.home / "tapes",
            store=store if is_async_tape_store(store) else AsyncTapeStoreAdapter(store),
            context=TapeContext(),
        ).session_tape(
            _bub_session_id(self.settings.channel_name, session.session_id),
            session.cwd,
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
                await client.session_update(session_id, UserMessageChunk(content=block))


async def run_acp_agent(
    framework: BubFramework, *, use_unstable_protocol: bool = True
) -> None:
    agent = BubACPAgent(framework, use_unstable_protocol=use_unstable_protocol)
    async with framework.running():
        get_steering_inbox = getattr(framework, "get_steering_inbox", None)
        if callable(get_steering_inbox):
            steering_inbox = get_steering_inbox()
            if isinstance(steering_inbox, ACPSteeringInbox):
                agent.set_steering_inbox(steering_inbox)
        try:
            await run_agent(agent, use_unstable_protocol=use_unstable_protocol)
        finally:
            await agent.shutdown()


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


#: Client identity the Lody app sends in `InitializeRequest.clientInfo`:
#: `{name: "lody", title: "Lody", version: "1"}`. Matching is prefix-based on
#: `name` so variants such as `lody-cli` are still recognized.
_LODY_CLIENT_NAME = "lody"


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


#: Quotation pairs a model may wrap a title in.
_QUOTE_PAIRS = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019", "`": "`"}


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


def _subagent_session_id(strategy: str, parent_session_id: str) -> str:
    """Mirror Bub's `subagent` session strategy."""

    if strategy == "inherit":
        return parent_session_id or f"temp/{uuid4().hex[:8]}"
    if strategy == "temp":
        return f"temp/{uuid4().hex[:8]}"
    return strategy


def _subagent_description(prompt: object) -> str | None:
    if not isinstance(prompt, str):
        return None
    text = prompt.strip()
    if not text:
        return None
    return text.splitlines()[0].strip()[:200] or None


def _subagent_total_tokens(usage: object) -> int | None:
    if not isinstance(usage, Mapping):
        return None
    return _usage_total_tokens(usage)


def _summary(text: str) -> str | None:
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line[:200] or None


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
