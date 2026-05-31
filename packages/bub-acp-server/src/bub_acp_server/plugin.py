from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import re
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import bub
import typer
from acp import (
    run_agent,
    text_block,
    update_agent_message_text,
    update_user_message,
    update_user_message_text,
)
from acp.interfaces import Client
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
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionInfo,
    SessionListCapabilities,
    SessionResumeCapabilities,
    SseMcpServer,
    TextContentBlock,
    ToolKind,
    ResumeSessionResponse,
)
from acp.helpers import start_tool_call, tool_content, update_tool_call
from bub import hookimpl
from bub.channels.message import ChannelMessage, MediaItem, MediaType
from bub.envelope import content_of, field_of
from bub.types import Envelope, OutboundChannelRouter, TurnResult
from republic import StreamEvent, TapeEntry, TapeQuery

from bub_acp_server.config import ACPServerSettings

if TYPE_CHECKING:
    from bub.framework import BubFramework

type ACPPromptBlock = (
    TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock
)
type ACPMcpServer = HttpMcpServer | SseMcpServer | McpServerStdio
type StreamPayload = Mapping[str, object]

_BUB_PROMPT_CONTEXT = re.compile(r"^acp_session_id=[^\n]+\n---Date: [^\n]+---\n", re.MULTILINE)
_CONTINUATION_PROMPT_PREFIX = "Continue the task until all targets are completed."


@dataclass(slots=True)
class ACPSession:
    session_id: str
    cwd: Path
    additional_directories: list[str] = field(default_factory=list)
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
        return cls(
            session_id=session_id,
            cwd=Path(cwd).expanduser().resolve(),
            additional_directories=[str(item) for item in additional_directories if isinstance(item, str)],
            title=title if isinstance(title, str) else None,
            updated_at=updated_at if isinstance(updated_at, str) else None,
        )


class ACPStreamRouter:
    def __init__(self, client: Client, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._tool_ids: dict[int, str] = {}
        self._sent_text = False

    @property
    def sent_text(self) -> bool:
        return self._sent_text

    def wrap_stream(self, message: Envelope, stream: AsyncIterable[StreamEvent]) -> AsyncIterable[StreamEvent]:
        del message

        async def iterator() -> AsyncIterator[StreamEvent]:
            async for event in stream:
                await self._publish_stream_event(event)
                yield event

        return iterator()

    async def dispatch_output(self, message: Envelope) -> bool:
        if field_of(message, "kind") == "error":
            await self._send_agent_text(content_of(message))
        return True

    async def quit(self, session_id: str) -> None:
        del session_id

    async def _publish_stream_event(self, event: StreamEvent) -> None:
        if event.kind == "text":
            delta = str(event.data.get("delta", ""))
            if delta:
                self._sent_text = True
                await self._send_agent_text(delta)
        elif event.kind == "user_text":
            delta = str(event.data.get("delta", ""))
            if delta:
                await self._send_user_text(delta)
        elif event.kind == "tool_call":
            await self._send_tool_call(event.data)
        elif event.kind == "tool_result":
            await self._send_tool_result(event.data)
        elif event.kind == "error":
            message = event.data.get("message") or event.data.get("error") or "unknown error"
            await self._send_agent_text(f"\nError: {message}")

    async def _send_agent_text(self, text: str) -> None:
        if not text:
            return
        await self._client.session_update(self._session_id, update_agent_message_text(text))

    async def _send_user_text(self, text: str) -> None:
        if not text:
            return
        await self._client.session_update(self._session_id, update_user_message_text(text))

    async def _send_tool_call(self, data: StreamPayload) -> None:
        index = _int_value(data.get("index"), default=len(self._tool_ids))
        call = data.get("call")
        tool_id = _tool_call_id(index, call)
        self._tool_ids[index] = tool_id
        title = _tool_title(call)
        await self._client.session_update(
            self._session_id,
            start_tool_call(
                tool_id,
                title,
                kind=_tool_kind(title),
                status="in_progress",
                raw_input=call,
            ),
        )

    async def _send_tool_result(self, data: StreamPayload) -> None:
        index = _int_value(data.get("index"), default=0)
        tool_id = self._tool_ids.get(index, f"tool-{index}")
        result = data.get("result")
        await self._client.session_update(
            self._session_id,
            update_tool_call(
                tool_id,
                status="completed",
                raw_output=result,
                content=[tool_content(text_block(_stringify(result)))],
            ),
        )


class BubACPAgent:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework
        self.settings = bub.ensure_config(ACPServerSettings)
        self._client: Client | None = None
        self._session_store_path = bub.home.expanduser() / "acp-sessions.json"
        self._sessions: dict[str, ACPSession] = self._load_sessions()
        self._prompt_lock = asyncio.Lock()

    def on_connect(self, conn: Client) -> None:
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del client_capabilities, client_info, kwargs
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_info=Implementation(name="bub", title="Bub", version="0.1.0"),
            agent_capabilities=AgentCapabilities(
                load_session=True,
                session_capabilities=SessionCapabilities(
                    close=SessionCloseCapabilities(),
                    list=SessionListCapabilities(),
                    resume=SessionResumeCapabilities(),
                )
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
        return NewSessionResponse(session_id=session_id)

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
        return LoadSessionResponse()

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[ACPMcpServer] | None = None,
        **kwargs: Any,
    ) -> ResumeSessionResponse:
        del mcp_servers, kwargs
        self._load_or_adopt_session(
            session_id=session_id,
            cwd=cwd,
            additional_directories=additional_directories,
        )
        return ResumeSessionResponse()

    async def list_sessions(
        self,
        additional_directories: list[str] | None = None,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> ListSessionsResponse:
        del additional_directories, cursor, cwd, kwargs
        self._sessions = self._load_sessions()
        sessions = sorted(self._sessions.values(), key=lambda item: item.updated_at or "", reverse=True)
        return ListSessionsResponse(sessions=[session.info() for session in sessions])

    async def close_session(self, session_id: str, **kwargs: Any) -> CloseSessionResponse | None:
        del kwargs
        self._sessions.pop(session_id, None)
        self._save_sessions()
        return CloseSessionResponse()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        await self.framework.quit_via_router(session_id)

    async def prompt(
        self,
        prompt: list[ACPPromptBlock],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> PromptResponse:
        del kwargs
        client = self._require_client()
        session = self._sessions.get(session_id) or self._adopt_session(session_id)
        session.touch()
        self._save_sessions()

        content, media = _prompt_to_bub_content(prompt)
        inbound = ChannelMessage(
            session_id=session_id,
            channel=self.settings.channel_name,
            chat_id=session_id,
            content=content,
            is_active=True,
            kind="normal",
            media=media,
            context={"acp_session_id": session_id},
        )
        if self.settings.send_user_message_updates:
            await self._send_user_message_updates(prompt, session_id)

        result = await self._process_inbound_with_streaming(inbound, session, client)
        if not result.model_output:
            return PromptResponse(stop_reason="end_turn", user_message_id=message_id)
        return PromptResponse(stop_reason="end_turn", user_message_id=message_id)

    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("ACP client is not connected")
        return self._client

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
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self._session_store_path)

    async def _attach_session_history(self, session: ACPSession) -> None:
        client = self._require_client()
        router = ACPStreamRouter(client, session.session_id)
        inbound = ChannelMessage(
            session_id=session.session_id,
            channel=self.settings.channel_name,
            chat_id=session.session_id,
            content="",
            is_active=False,
            kind="normal",
            context={"acp_session_id": session.session_id},
        )
        async for _ in router.wrap_stream(inbound, self._session_history_stream(session)):
            pass

    async def _session_history_stream(self, session: ACPSession) -> AsyncIterator[StreamEvent]:
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
                    tool_index = pending_tool_indices[index] if index < len(pending_tool_indices) else index
                    yield StreamEvent("tool_result", {"index": tool_index, "result": result})
                pending_tool_indices = []
            elif entry.kind == "error":
                yield StreamEvent("error", {"message": _stringify(entry.payload.get("message") or entry.payload)})

    async def _load_tape_entries(self, session: ACPSession) -> list[TapeEntry]:
        tape_name = _session_tape_name(session.session_id, session.cwd)
        store = _framework_tape_store(self.framework)
        if store is not None:
            query = TapeQuery(tape_name, store)
            with contextlib.suppress(Exception):
                result = store.fetch_all(query)
                if inspect.isawaitable(result):
                    result = await result
                return list(cast(Iterable[TapeEntry], result))
        return _load_tape_entries_from_file(bub.home.expanduser() / "tapes" / f"{tape_name}.jsonl")

    async def _send_user_message_updates(self, prompt: list[ACPPromptBlock], session_id: str) -> None:
        client = self._require_client()
        for block in prompt:
            if _block_type(block) == "text":
                await client.session_update(session_id, update_user_message(block))

    async def _process_inbound_with_streaming(
        self,
        inbound: ChannelMessage,
        session: ACPSession,
        client: Client,
    ) -> TurnResult:
        async with self._prompt_lock:
            router = ACPStreamRouter(client, session.session_id)
            previous_router = cast(
                OutboundChannelRouter | None,
                getattr(self.framework, "_outbound_router", None),
            )
            previous_workspace = self.framework.workspace
            self.framework.workspace = session.cwd
            self.framework.bind_outbound_router(router)
            try:
                result = await self.framework.process_inbound(inbound, stream_output=True)
            finally:
                self.framework.bind_outbound_router(previous_router)
                self.framework.workspace = previous_workspace
            if result.model_output and not router.sent_text:
                await client.session_update(session.session_id, update_agent_message_text(result.model_output))
            return result


async def run_acp_agent(framework: BubFramework, *, use_unstable_protocol: bool = True) -> None:
    async with framework.running():
        await run_agent(BubACPAgent(framework), use_unstable_protocol=use_unstable_protocol)


class ACPServerPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        acp_app = typer.Typer(name="acp", help="Run Bub as an ACP agent.", add_completion=False)

        @acp_app.command("serve")
        def serve() -> None:
            asyncio.run(run_acp_agent(self.framework))

        app.add_typer(acp_app, name="acp")


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
    candidate = _block_value(call, "id", None) or _block_value(call, "tool_call_id", None)
    return str(candidate or f"tool-{index}")


def _tool_title(call: object) -> str:
    name = _block_value(call, "name", None)
    if name is None:
        function = _block_value(call, "function", None)
        name = _block_value(function, "name", None)
    return str(name or "tool")


def _tool_kind(title: str) -> ToolKind:
    lower_title = title.lower()
    if any(token in lower_title for token in ("read", "cat", "view")):
        return "read"
    if any(token in lower_title for token in ("write", "edit", "patch")):
        return "edit"
    if any(token in lower_title for token in ("delete", "remove", "rm")):
        return "delete"
    if any(token in lower_title for token in ("search", "grep", "rg")):
        return "search"
    if any(token in lower_title for token in ("bash", "shell", "exec", "run")):
        return "execute"
    return "other"


def _int_value(value: object, *, default: int) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return int(value)
    return default


def _framework_tape_store(framework: BubFramework) -> object | None:
    get_tape_store = getattr(framework, "get_tape_store", None)
    if get_tape_store is None:
        return None
    store = get_tape_store()
    return store if hasattr(store, "fetch_all") else None


def _session_tape_name(session_id: str, workspace: Path) -> str:
    workspace_hash = hashlib.md5(str(workspace.resolve()).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    session_hash = hashlib.md5(session_id.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
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
    if not isinstance(entry_id, int) or not isinstance(kind, str) or not isinstance(entry_payload, dict):
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
