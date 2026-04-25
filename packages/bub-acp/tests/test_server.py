from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import acp
import pytest
from acp import schema
from republic import AsyncStreamEvents, StreamEvent

from bub_acp.server import BubACPServerAgent


class FakeRunner:
    async def run_stream(
        self,
        *,
        session_id: str,
        prompt: str | list[dict[str, object]],
        cwd: Path,
    ) -> AsyncStreamEvents:
        async def iterator() -> AsyncIterator[StreamEvent]:
            yield StreamEvent("text", {"delta": "hello"})
            yield StreamEvent(
                "tool_call",
                {"index": 0, "call": {"id": "tool-1", "title": "scan", "kind": "search", "status": "in_progress"}},
            )
            yield StreamEvent(
                "tool_result",
                {"index": 0, "result": {"id": "tool-1", "title": "scan", "kind": "search", "status": "completed", "raw_output": {"ok": True}}},
            )
            yield StreamEvent("usage", {"used": 12, "size": 256})
            yield StreamEvent("final", {"text": "hello", "ok": True})

        return AsyncStreamEvents(iterator())


class CollectingClient(acp.Client):
    def __init__(self) -> None:
        self.updates: list[object] = []

    async def session_update(self, session_id: str, update: object, **kwargs: object) -> None:
        self.updates.append(update)

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        raise NotImplementedError

    async def read_text_file(self, path, session_id, limit=None, line=None, **kwargs):
        raise NotImplementedError

    async def write_text_file(self, content, path, session_id, **kwargs):
        raise NotImplementedError

    async def create_terminal(self, command, session_id, args=None, cwd=None, env=None, output_byte_limit=None, **kwargs):
        raise NotImplementedError

    async def terminal_output(self, session_id, terminal_id, **kwargs):
        raise NotImplementedError

    async def wait_for_terminal_exit(self, session_id, terminal_id, **kwargs):
        raise NotImplementedError

    async def kill_terminal(self, session_id, terminal_id, **kwargs):
        raise NotImplementedError

    async def release_terminal(self, session_id, terminal_id, **kwargs):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_server_streams_runner_updates(tmp_path: Path) -> None:
    server = BubACPServerAgent(runner=FakeRunner())
    client = CollectingClient()
    server.on_connect(client)

    init = await server.initialize(protocol_version=1)
    session = await server.new_session(cwd=str(tmp_path))
    response = await server.prompt(
        prompt=[acp.text_block("hello world")],
        session_id=session.session_id,
        message_id="user-1",
    )

    assert init.agent_info is not None
    assert response.stop_reason == "end_turn"
    assert response.user_message_id == "user-1"
    assert any(isinstance(update, schema.AgentMessageChunk) for update in client.updates)
    assert any(isinstance(update, schema.ToolCallStart) for update in client.updates)
    assert any(isinstance(update, schema.ToolCallProgress) for update in client.updates)
    assert any(isinstance(update, schema.UsageUpdate) for update in client.updates)


@pytest.mark.asyncio
async def test_server_session_lifecycle(tmp_path: Path) -> None:
    server = BubACPServerAgent(runner=FakeRunner())
    created = await server.new_session(cwd=str(tmp_path))
    listing = await server.list_sessions(cwd=str(tmp_path))
    loaded = await server.load_session(cwd=str(tmp_path), session_id=created.session_id)
    resumed = await server.resume_session(cwd=str(tmp_path), session_id=created.session_id)
    forked = await server.fork_session(cwd=str(tmp_path), session_id=created.session_id)
    closed = await server.close_session(created.session_id)

    assert loaded is not None
    assert resumed is not None
    assert closed is not None
    assert any(item.session_id == created.session_id for item in listing.sessions)
    assert forked.session_id != created.session_id
