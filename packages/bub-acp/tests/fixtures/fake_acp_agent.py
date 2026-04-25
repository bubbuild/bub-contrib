from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import acp
from acp import schema


def _sessions_path(cwd: Path) -> Path:
    return cwd / ".fake-acp-sessions.json"


def _read_sessions(cwd: Path) -> dict[str, Any]:
    path = _sessions_path(cwd)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_sessions(cwd: Path, payload: dict[str, Any]) -> None:
    _sessions_path(cwd).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _text_from_prompt(prompt: list[Any]) -> str:
    parts: list[str] = []
    for block in prompt:
        if isinstance(block, schema.TextContentBlock):
            parts.append(block.text)
        elif isinstance(block, schema.ImageContentBlock):
            parts.append(f"[image:{block.mime_type}]")
        else:
            parts.append(str(block))
    return "\n".join(parts).strip()


class FakeAgent(acp.Agent):
    def __init__(self) -> None:
        self.client: acp.Client | None = None
        self.session_dirs: dict[str, Path] = {}

    def on_connect(self, conn: acp.Client) -> None:
        self.client = conn

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        return schema.InitializeResponse(
            protocol_version=protocol_version,
            agent_info=schema.Implementation(name="fake-acp", title="Fake ACP", version="0.1.0"),
            agent_capabilities=schema.AgentCapabilities(
                load_session=True,
                prompt_capabilities=schema.PromptCapabilities(image=True),
                session_capabilities=schema.SessionCapabilities(
                    list=schema.SessionListCapabilities(),
                    resume=schema.SessionResumeCapabilities(),
                    fork=schema.SessionForkCapabilities(),
                    close=schema.SessionCloseCapabilities(),
                ),
            ),
        )

    async def new_session(self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any) -> schema.NewSessionResponse:
        session_id = str(uuid.uuid4())
        root = Path(cwd).resolve()
        sessions = _read_sessions(root)
        sessions[session_id] = {"turns": 0}
        _write_sessions(root, sessions)
        self.session_dirs[session_id] = root
        return schema.NewSessionResponse(session_id=session_id)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.LoadSessionResponse | None:
        root = Path(cwd).resolve()
        sessions = _read_sessions(root)
        if session_id not in sessions:
            return None
        self.session_dirs[session_id] = root
        return schema.LoadSessionResponse()

    async def list_sessions(self, cursor: str | None = None, cwd: str | None = None, **kwargs: Any) -> schema.ListSessionsResponse:
        root = Path(cwd or ".").resolve()
        sessions = _read_sessions(root)
        return schema.ListSessionsResponse(
            sessions=[
                schema.SessionInfo(
                    session_id=session_id,
                    cwd=str(root),
                    title=f"fake-{session_id[:8]}",
                )
                for session_id in sessions
            ]
        )

    async def resume_session(self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None, **kwargs: Any) -> schema.ResumeSessionResponse:
        loaded = await self.load_session(cwd, session_id, mcp_servers)
        if loaded is None:
            raise KeyError(session_id)
        return schema.ResumeSessionResponse()

    async def fork_session(self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None, **kwargs: Any) -> schema.ForkSessionResponse:
        root = Path(cwd).resolve()
        sessions = _read_sessions(root)
        if session_id not in sessions:
            raise KeyError(session_id)
        forked = str(uuid.uuid4())
        sessions[forked] = dict(sessions[session_id])
        _write_sessions(root, sessions)
        self.session_dirs[forked] = root
        return schema.ForkSessionResponse(session_id=forked)

    async def close_session(self, session_id: str, **kwargs: Any) -> schema.CloseSessionResponse:
        return schema.CloseSessionResponse()

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> schema.PromptResponse:
        root = self.session_dirs[session_id]
        sessions = _read_sessions(root)
        entry = sessions[session_id]
        entry["turns"] = int(entry.get("turns", 0)) + 1
        _write_sessions(root, sessions)

        prompt_text = _text_from_prompt(prompt)
        reply_parts = [f"turn={entry['turns']}", f"prompt={prompt_text}"]

        if self.client is not None:
            await self.client.session_update(
                session_id,
                acp.start_tool_call(
                    tool_call_id="prep",
                    title="prepare reply",
                    kind="other",
                    status="in_progress",
                    raw_input={"prompt": prompt_text},
                ),
            )

        if "permission" in prompt_text and self.client is not None:
            decision = await self.client.request_permission(
                options=[
                    schema.PermissionOption(option_id="allow", name="Allow once", kind="allow_once"),
                    schema.PermissionOption(option_id="deny", name="Reject once", kind="reject_once"),
                ],
                session_id=session_id,
                tool_call=acp.update_tool_call("prep", title="permission", status="pending"),
            )
            reply_parts.append(f"permission={decision.outcome.outcome}")

        if "filesystem" in prompt_text and self.client is not None:
            await self.client.write_text_file("hello from fake agent\n", "notes/output.txt", session_id)
            read_back = await self.client.read_text_file("notes/output.txt", session_id)
            reply_parts.append(f"file={read_back.content.strip()}")

        if "terminal" in prompt_text and self.client is not None:
            terminal = await self.client.create_terminal(
                sys.executable,
                session_id,
                args=["-c", "print('terminal ok')"],
            )
            await self.client.wait_for_terminal_exit(session_id, terminal.terminal_id)
            output = await self.client.terminal_output(session_id, terminal.terminal_id)
            await self.client.release_terminal(session_id, terminal.terminal_id)
            reply_parts.append(f"terminal={output.output.strip()}")

        reply_text = " ".join(reply_parts)
        if self.client is not None:
            await self.client.session_update(
                session_id,
                schema.AgentMessageChunk(
                    session_update="agent_message_chunk",
                    message_id=str(uuid.uuid4()),
                    content=acp.text_block(reply_text),
                ),
            )
            await self.client.session_update(
                session_id,
                acp.update_tool_call(
                    "prep",
                    status="completed",
                    raw_output={"reply": reply_text},
                ),
            )
            await self.client.session_update(
                session_id,
                schema.UsageUpdate(
                    session_update="usage_update",
                    used=entry["turns"] * 10,
                    size=1024,
                ),
            )
        return schema.PromptResponse(
            stop_reason="end_turn",
            user_message_id=message_id,
        )


if __name__ == "__main__":
    asyncio.run(acp.run_agent(FakeAgent()))
