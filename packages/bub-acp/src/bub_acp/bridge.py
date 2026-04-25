from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import acp
from acp import schema
from bub.types import State
from republic import AsyncStreamEvents, RepublicError, StreamEvent, StreamState

from bub_acp.config import ACPAgentProcessConfig, ACPSettings

if TYPE_CHECKING:
    from acp.client.connection import ClientSideConnection
    from bub.builtin.agent import Agent

ACP_PROTOCOL_VERSION = 1
ACP_SESSION_FILE = ".bub-acp-sessions.json"
TERMINAL_OUTPUT_LIMIT = 64 * 1024


def workspace_from_state(state: State) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _runtime_agent_from_state(state: State) -> Agent | None:
    agent = state.get("_runtime_agent")
    if agent is None:
        return None
    return cast("Agent", agent)


def _client_capabilities() -> schema.ClientCapabilities:
    return schema.ClientCapabilities(
        fs=schema.FileSystemCapabilities(
            read_text_file=True,
            write_text_file=True,
        ),
        terminal=True,
    )


def _client_info() -> schema.Implementation:
    return schema.Implementation(name="bub-acp", title="Bub ACP Bridge", version="0.1.0")


def _render_block_as_text(block: Any) -> str:
    if isinstance(block, schema.TextContentBlock):
        return block.text
    if isinstance(block, schema.ImageContentBlock):
        return f"[image:{block.mime_type}]"
    if isinstance(block, schema.AudioContentBlock):
        return f"[audio:{block.mime_type}]"
    if isinstance(block, schema.ResourceContentBlock):
        return f"[resource:{block.name} {block.uri}]"
    if isinstance(block, schema.EmbeddedResourceContentBlock):
        resource = block.resource
        if isinstance(resource, schema.TextResourceContents):
            return resource.text
        if isinstance(resource, schema.BlobResourceContents):
            return f"[resource:{resource.mime_type}]"
    return str(block)


def prompt_to_acp_blocks(prompt: str | list[dict[str, Any]]) -> list[Any]:
    if isinstance(prompt, str):
        return [acp.text_block(prompt)]

    blocks: list[Any] = []
    for item in prompt:
        part_type = item.get("type")
        if part_type == "text":
            text = str(item.get("text", ""))
            if text:
                blocks.append(acp.text_block(text))
            continue
        if part_type == "image_url":
            image = item.get("image_url")
            if not isinstance(image, dict):
                continue
            url = image.get("url")
            if not isinstance(url, str) or not url.startswith("data:"):
                blocks.append(acp.text_block("[unsupported image reference]"))
                continue
            header, encoded = url.split(",", 1)
            mime_type = header.removeprefix("data:").split(";", 1)[0]
            blocks.append(acp.image_block(encoded, mime_type))
            continue
        blocks.append(acp.text_block(json.dumps(item, ensure_ascii=False)))
    return blocks or [acp.text_block("")]


def prompt_from_acp_blocks(blocks: list[Any]) -> str | list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    only_text = True
    for block in blocks:
        if isinstance(block, schema.TextContentBlock):
            parts.append({"type": "text", "text": block.text})
            continue
        if isinstance(block, schema.ImageContentBlock):
            only_text = False
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{block.mime_type};base64,{block.data}",
                    },
                }
            )
            continue
        parts.append({"type": "text", "text": _render_block_as_text(block)})
    if only_text:
        return "\n\n".join(part["text"] for part in parts if part["text"]).strip()
    return parts


def _merge_env(base: Mapping[str, str], overrides: Mapping[str, str]) -> dict[str, str]:
    merged = dict(base)
    merged.update(overrides)
    return merged


def _tool_call_payload(update: schema.ToolCallStart | schema.ToolCallProgress) -> dict[str, Any]:
    payload = {
        "id": update.tool_call_id,
        "title": update.title,
        "kind": update.kind,
        "status": update.status,
        "raw_input": update.raw_input,
        "raw_output": update.raw_output,
    }
    if update.content is not None:
        payload["content"] = [item.model_dump(mode="json", by_alias=True) for item in update.content]
    if update.locations is not None:
        payload["locations"] = [item.model_dump(mode="json", by_alias=True) for item in update.locations]
    return payload


def _tool_result_payload(update: schema.ToolCallProgress) -> dict[str, Any]:
    payload = {
        "id": update.tool_call_id,
        "status": update.status,
        "title": update.title,
        "kind": update.kind,
        "raw_output": update.raw_output,
    }
    if update.content is not None:
        payload["content"] = [item.model_dump(mode="json", by_alias=True) for item in update.content]
    return payload


def _usage_payload(update: schema.UsageUpdate) -> dict[str, Any]:
    payload = {"used": update.used, "size": update.size}
    if update.cost is not None:
        payload["cost"] = update.cost.model_dump(mode="json", by_alias=True)
    return payload


def _session_map_path(workspace: Path) -> Path:
    return workspace / ACP_SESSION_FILE


def _load_session_map(workspace: Path) -> dict[str, str]:
    path = _session_map_path(workspace)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _save_session_map(workspace: Path, mapping: dict[str, str]) -> None:
    path = _session_map_path(workspace)
    path.write_text(json.dumps(mapping, indent=2, sort_keys=True), encoding="utf-8")


def _session_map_key(agent_name: str, session_id: str) -> str:
    return f"{agent_name}:{session_id}"


def _inside_workspace(workspace: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return False
    return True


def _signal_name(returncode: int) -> str | None:
    if returncode >= 0:
        return None
    with contextlib.suppress(ValueError):
        return signal.Signals(-returncode).name
    return f"SIG{returncode}"


@dataclass
class TerminalSession:
    process: asyncio.subprocess.Process
    output_limit: int
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    reader_task: asyncio.Task[None] | None = None

    async def wait(self) -> int:
        return await self.process.wait()

    def snapshot(self) -> str:
        return self.output.decode("utf-8", errors="replace")

    def exit_status(self) -> schema.TerminalExitStatus | None:
        if self.process.returncode is None:
            return None
        return schema.TerminalExitStatus(
            exit_code=self.process.returncode if self.process.returncode >= 0 else None,
            signal=_signal_name(self.process.returncode),
        )


class ACPClientHost(acp.Client):
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._updates: dict[str, asyncio.Queue[Any]] = {}
        self._terminals: dict[str, TerminalSession] = {}

    def open_update_queue(self, session_id: str) -> asyncio.Queue[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._updates[session_id] = queue
        return queue

    def close_update_queue(self, session_id: str) -> None:
        self._updates.pop(session_id, None)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        queue = self._updates.get(session_id)
        if queue is not None:
            await queue.put(update)

    async def request_permission(
        self,
        options: list[schema.PermissionOption],
        session_id: str,
        tool_call: schema.ToolCallProgress,
        **kwargs: Any,
    ) -> schema.RequestPermissionResponse:
        for option in options:
            if option.kind in {"allow_once", "allow_always"}:
                return schema.RequestPermissionResponse(
                    outcome=schema.AllowedOutcome(option_id=option.option_id, outcome="selected")
                )
        return schema.RequestPermissionResponse(outcome=schema.DeniedOutcome(outcome="cancelled"))

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: Any,
    ) -> schema.ReadTextFileResponse:
        resolved = self._resolve_path(path)
        content = resolved.read_text(encoding="utf-8")
        if line is not None:
            lines = content.splitlines(keepends=True)
            content = "".join(lines[max(line - 1, 0) :])
        if limit is not None:
            content = content[:limit]
        return schema.ReadTextFileResponse(content=content)

    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,
        **kwargs: Any,
    ) -> schema.WriteTextFileResponse:
        resolved = self._resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return schema.WriteTextFileResponse()

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[schema.EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> schema.CreateTerminalResponse:
        terminal_id = str(uuid.uuid4())
        env_map = os.environ.copy()
        if env:
            env_map.update({item.name: item.value for item in env})
        working_directory = self.workspace if cwd is None else self._resolve_path(cwd, allow_directory=True)
        process = await asyncio.create_subprocess_exec(
            command,
            *(args or []),
            cwd=str(working_directory),
            env=env_map,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        session = TerminalSession(
            process=process,
            output_limit=output_byte_limit or TERMINAL_OUTPUT_LIMIT,
        )
        session.reader_task = asyncio.create_task(self._capture_terminal_output(session))
        self._terminals[terminal_id] = session
        return schema.CreateTerminalResponse(terminal_id=terminal_id)

    async def terminal_output(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> schema.TerminalOutputResponse:
        terminal = self._require_terminal(terminal_id)
        return schema.TerminalOutputResponse(
            output=terminal.snapshot(),
            truncated=terminal.truncated,
            exit_status=terminal.exit_status(),
        )

    async def wait_for_terminal_exit(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> schema.WaitForTerminalExitResponse:
        terminal = self._require_terminal(terminal_id)
        await terminal.wait()
        if terminal.reader_task is not None:
            await terminal.reader_task
        status = terminal.exit_status()
        return schema.WaitForTerminalExitResponse(
            exit_code=None if status is None else status.exit_code,
            signal=None if status is None else status.signal,
        )

    async def kill_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> schema.KillTerminalResponse:
        terminal = self._require_terminal(terminal_id)
        if terminal.process.returncode is None:
            terminal.process.kill()
            await terminal.process.wait()
        if terminal.reader_task is not None:
            await terminal.reader_task
        return schema.KillTerminalResponse()

    async def release_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> schema.ReleaseTerminalResponse:
        terminal = self._terminals.pop(terminal_id, None)
        if terminal is not None:
            if terminal.process.returncode is None:
                terminal.process.terminate()
                with contextlib.suppress(ProcessLookupError):
                    await terminal.process.wait()
            if terminal.reader_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await terminal.reader_task
        return schema.ReleaseTerminalResponse()

    async def _capture_terminal_output(self, terminal: TerminalSession) -> None:
        stream = terminal.process.stdout
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            remaining = terminal.output_limit - len(terminal.output)
            if remaining <= 0:
                terminal.truncated = True
                continue
            if len(chunk) > remaining:
                terminal.output.extend(chunk[:remaining])
                terminal.truncated = True
                continue
            terminal.output.extend(chunk)

    def _resolve_path(self, raw_path: str, *, allow_directory: bool = False) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        if not _inside_workspace(self.workspace, resolved):
            raise ValueError(f"path escapes workspace: {raw_path}")
        if resolved.exists() and not allow_directory and resolved.is_dir():
            raise IsADirectoryError(raw_path)
        return resolved

    def _require_terminal(self, terminal_id: str) -> TerminalSession:
        if terminal_id not in self._terminals:
            raise KeyError(terminal_id)
        return self._terminals[terminal_id]


class ACPBridge:
    def __init__(self, settings: ACPSettings | None = None) -> None:
        self.settings = settings or ACPSettings()

    async def run_model(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        session_id: str,
        state: State,
    ) -> str | None:
        stream = await self.run_model_stream(prompt, session_id=session_id, state=state)
        if stream is None:
            return None
        parts: list[str] = []
        async for event in stream:
            if event.kind == "text":
                parts.append(str(event.data.get("delta", "")))
        return "".join(parts)

    async def run_model_stream(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        session_id: str,
        state: State,
    ) -> AsyncStreamEvents | None:
        internal = await self._run_internal_command(prompt, session_id=session_id, state=state)
        if internal is not None:
            return internal

        config = self.settings.read_config()
        if config.default_agent is None:
            return None
        agent = config.agents.get(config.default_agent)
        if agent is None:
            return None

        stream_state = StreamState()
        iterator = self._stream_external_agent(
            config.default_agent,
            agent,
            prompt,
            session_id=session_id,
            state=state,
            stream_state=stream_state,
        )
        return AsyncStreamEvents(iterator, state=stream_state)

    async def _run_internal_command(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: State,
    ) -> AsyncStreamEvents | None:
        if not isinstance(prompt, str) or not prompt.strip().startswith(","):
            return None
        agent = _runtime_agent_from_state(state)
        if agent is None:
            return None
        result = await agent.run(session_id=session_id, prompt=prompt, state=state)

        async def iterator() -> AsyncIterator[StreamEvent]:
            yield StreamEvent("text", {"delta": result})
            yield StreamEvent("final", {"text": result, "tool_calls": [], "tool_results": [], "ok": True})

        return AsyncStreamEvents(iterator())

    async def _stream_external_agent(
        self,
        agent_name: str,
        agent: ACPAgentProcessConfig,
        prompt: str | list[dict[str, Any]],
        *,
        session_id: str,
        state: State,
        stream_state: StreamState,
    ) -> AsyncIterator[StreamEvent]:
        workspace = workspace_from_state(state)
        workspace.mkdir(parents=True, exist_ok=True)
        host = ACPClientHost(workspace)
        remote_session_id: str | None = None
        queue: asyncio.Queue[Any] | None = None
        tool_indexes: dict[str, int] = {}
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        text_parts: list[str] = []
        usage: dict[str, Any] | None = None

        command = [agent.command, *agent.args]
        try:
            async with acp.spawn_agent_process(
                host,
                *command,
                env=_merge_env(os.environ, agent.env),
                cwd=agent.cwd or str(workspace),
            ) as (connection, _process):
                await connection.initialize(
                    protocol_version=ACP_PROTOCOL_VERSION,
                    client_capabilities=_client_capabilities(),
                    client_info=_client_info(),
                )
                remote_session_id = await self._resolve_remote_session(
                    connection,
                    workspace,
                    agent_name=agent_name,
                    session_id=session_id,
                )
                queue = host.open_update_queue(remote_session_id)
                response_task = asyncio.create_task(
                    connection.prompt(
                        prompt=prompt_to_acp_blocks(prompt),
                        session_id=remote_session_id,
                        message_id=str(uuid.uuid4()),
                    )
                )
                while True:
                    try:
                        timeout = 0.1 if response_task.done() else 0.05
                        update = await asyncio.wait_for(queue.get(), timeout=timeout)
                    except TimeoutError:
                        if response_task.done():
                            break
                        continue
                    for event in self._stream_events_from_update(
                        update,
                        tool_indexes=tool_indexes,
                        tool_calls=tool_calls,
                        tool_results=tool_results,
                    ):
                        if event.kind == "text":
                            text_parts.append(str(event.data.get("delta", "")))
                        elif event.kind == "usage":
                            usage = event.data
                            stream_state.usage = usage
                        yield event

                response = await response_task
                final_text = "".join(text_parts)
                final_event = StreamEvent(
                    "final",
                    {
                        "text": final_text,
                        "tool_calls": tool_calls,
                        "tool_results": tool_results,
                        "usage": usage,
                        "stop_reason": response.stop_reason,
                        "user_message_id": response.user_message_id,
                        "ok": response.stop_reason != "cancelled",
                    },
                )
                yield final_event
        except Exception as exc:
            error = RepublicError.from_exception(exc)
            stream_state.error = error
            yield StreamEvent("error", error.as_dict())
            yield StreamEvent(
                "final",
                {
                    "text": "".join(text_parts),
                    "tool_calls": tool_calls,
                    "tool_results": tool_results,
                    "usage": usage,
                    "ok": False,
                },
            )
        finally:
            if remote_session_id is not None and queue is not None:
                host.close_update_queue(remote_session_id)

    def _stream_events_from_update(
        self,
        update: Any,
        *,
        tool_indexes: dict[str, int],
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> list[StreamEvent]:
        if isinstance(update, schema.AgentMessageChunk):
            text = _render_block_as_text(update.content)
            return [StreamEvent("text", {"delta": text})] if text else []

        if isinstance(update, schema.ToolCallStart):
            index = tool_indexes.setdefault(update.tool_call_id, len(tool_indexes))
            payload = _tool_call_payload(update)
            tool_calls.append(payload)
            return [StreamEvent("tool_call", {"index": index, "call": payload})]

        if isinstance(update, schema.ToolCallProgress):
            index = tool_indexes.setdefault(update.tool_call_id, len(tool_indexes))
            if update.status in {"completed", "failed"}:
                payload = _tool_result_payload(update)
                tool_results.append(payload)
                return [StreamEvent("tool_result", {"index": index, "result": payload})]
            return []

        if isinstance(update, schema.UsageUpdate):
            return [StreamEvent("usage", _usage_payload(update))]

        return []

    async def _resolve_remote_session(
        self,
        connection: ClientSideConnection,
        workspace: Path,
        *,
        agent_name: str,
        session_id: str,
    ) -> str:
        mapping = _load_session_map(workspace)
        key = _session_map_key(agent_name, session_id)
        existing = mapping.get(key)
        if existing is not None:
            loaded = await connection.load_session(cwd=str(workspace), session_id=existing)
            if loaded is not None:
                return existing
        created = await connection.new_session(cwd=str(workspace))
        mapping[key] = created.session_id
        _save_session_map(workspace, mapping)
        return created.session_id
