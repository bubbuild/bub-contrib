from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from acp.schema import TextContentBlock
from bub.types import TurnResult
from republic import StreamEvent, TapeEntry, TapeQuery

from bub_acp_server import plugin
from bub_acp_server.plugin import BubACPAgent


@pytest.fixture(autouse=True)
def isolated_bub_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / ".bub"))


class FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(self, session_id: str, update: object, **kwargs: Any) -> None:
        self.updates.append((session_id, update))


class FakeFramework:
    def __init__(self) -> None:
        self.workspace = Path.cwd()
        self.router = None
        self.previous_routers: list[object] = []
        self.messages: list[object] = []
        self.stream_output_values: list[bool] = []

    def bind_outbound_router(self, router: object) -> None:
        self.previous_routers.append(router)
        self.router = router

    async def quit_via_router(self, session_id: str) -> None:
        return None

    async def process_inbound(self, inbound: object, stream_output: bool = False) -> TurnResult:
        self.messages.append(inbound)
        self.stream_output_values.append(stream_output)

        async def stream():
            yield StreamEvent("text", {"delta": "hello"})
            yield StreamEvent("tool_call", {"index": 0, "call": {"id": "call-1", "name": "bash"}})
            yield StreamEvent("tool_result", {"index": 0, "result": "ok"})
            yield StreamEvent("text", {"delta": " world"})
            yield StreamEvent("final", {"text": "hello world", "ok": True})

        async for _ in self.router.wrap_stream(inbound, stream()):
            pass
        return TurnResult(
            session_id=inbound.session_id,
            prompt=inbound.content,
            model_output="hello world",
        )


class FakeTapeStore:
    def __init__(self, entries: list[TapeEntry]) -> None:
        self.entries = entries
        self.queries: list[str] = []

    def fetch_all(self, query: TapeQuery) -> list[TapeEntry]:
        self.queries.append(query.tape)
        return self.entries


class TapeFramework(FakeFramework):
    def __init__(self, entries: list[TapeEntry]) -> None:
        super().__init__()
        self.tape_store = FakeTapeStore(entries)

    def get_tape_store(self) -> FakeTapeStore:
        return self.tape_store


class NoTextFramework(FakeFramework):
    async def process_inbound(self, inbound: object, stream_output: bool = False) -> TurnResult:
        self.messages.append(inbound)
        self.stream_output_values.append(stream_output)

        async def stream():
            yield StreamEvent("final", {"text": "late text", "ok": True})

        async for _ in self.router.wrap_stream(inbound, stream()):
            pass
        return TurnResult(session_id=inbound.session_id, prompt=inbound.content, model_output="late text")


@pytest.mark.asyncio
async def test_initialize_advertises_session_capabilities() -> None:
    agent = BubACPAgent(FakeFramework())
    response = await agent.initialize(protocol_version=1)

    assert response.protocol_version == 1
    assert response.agent_info is not None
    assert response.agent_info.name == "bub"
    assert response.agent_capabilities is not None
    assert response.agent_capabilities.session_capabilities is not None
    assert response.agent_capabilities.session_capabilities.list is not None
    assert response.agent_capabilities.session_capabilities.close is not None
    assert response.agent_capabilities.session_capabilities.resume is not None
    assert response.agent_capabilities.load_session is True


@pytest.mark.asyncio
async def test_resume_adopts_existing_editor_session_ids(tmp_path: Path) -> None:
    agent = BubACPAgent(FakeFramework())

    resume_response = await agent.resume_session(cwd=str(tmp_path), session_id="zed-session")
    sessions = await agent.list_sessions(cwd=str(tmp_path))

    assert resume_response is not None
    assert [session.session_id for session in sessions.sessions] == ["zed-session"]
    assert sessions.sessions[0].cwd == str(tmp_path)


@pytest.mark.asyncio
async def test_load_session_attaches_tape_history_through_streaming_router(tmp_path: Path) -> None:
    session_id = "zed-session"
    entries = [
        TapeEntry(
            1,
            "message",
            {
                "role": "user",
                "content": (
                    f"acp_session_id={session_id}|channel=$acp-server|chat_id={session_id}\n"
                    "---Date: 2026-06-01T03:42:01+08:00---\n"
                    "HELLO"
                ),
            },
        ),
        TapeEntry(2, "message", {"role": "assistant", "content": "Hi"}),
        TapeEntry(3, "tool_call", {"calls": [{"id": "call-1", "name": "bash"}]}),
        TapeEntry(4, "tool_result", {"results": ["ok"]}),
        TapeEntry(
            5,
            "message",
            {
                "role": "user",
                "content": "Continue the task until all targets are completed. [context: acp_session_id=x]",
            },
        ),
    ]
    framework = TapeFramework(entries)
    client = FakeClient()
    agent = BubACPAgent(framework)
    agent.on_connect(client)

    response = await agent.load_session(cwd=str(tmp_path), session_id=session_id)

    assert response is not None
    assert framework.tape_store.queries == [plugin._session_tape_name(session_id, tmp_path)]
    update_names = [update.session_update for _, update in client.updates]
    assert update_names == [
        "user_message_chunk",
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
    ]
    assert client.updates[0][1].content.text == "HELLO"
    assert client.updates[1][1].content.text == "Hi"


@pytest.mark.asyncio
async def test_sessions_survive_agent_restart(tmp_path: Path) -> None:
    first_agent = BubACPAgent(FakeFramework())
    created = await first_agent.new_session(cwd=str(tmp_path))

    second_agent = BubACPAgent(FakeFramework())
    sessions = await second_agent.list_sessions(cwd=str(tmp_path))

    assert [session.session_id for session in sessions.sessions] == [created.session_id]
    assert sessions.sessions[0].cwd == str(tmp_path)


@pytest.mark.asyncio
async def test_session_store_expands_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BUB_HOME", "~/.custom-bub")

    agent = BubACPAgent(FakeFramework())
    await agent.new_session(cwd=str(tmp_path))

    assert (tmp_path / ".custom-bub" / "acp-sessions.json").exists()


@pytest.mark.asyncio
async def test_run_acp_agent_registers_resume_routes_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class RunningFramework(FakeFramework):
        def running(self):
            class Context:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *args: object) -> None:
                    return None

            return Context()

    async def fake_run_agent(agent: object, *, use_unstable_protocol: bool = False) -> None:
        captured["agent"] = agent
        captured["use_unstable_protocol"] = use_unstable_protocol

    monkeypatch.setattr(plugin, "run_agent", fake_run_agent)

    await plugin.run_acp_agent(RunningFramework())

    assert isinstance(captured["agent"], BubACPAgent)
    assert captured["use_unstable_protocol"] is True


@pytest.mark.asyncio
async def test_prompt_streams_bub_events_to_acp_client() -> None:
    framework = FakeFramework()
    client = FakeClient()
    agent = BubACPAgent(framework)
    agent.on_connect(client)
    session = await agent.new_session(cwd=str(Path.cwd()))

    response = await agent.prompt(
        [TextContentBlock(type="text", text="say hello")],
        session_id=session.session_id,
        message_id="user-message-1",
    )

    assert response.stop_reason == "end_turn"
    assert response.user_message_id == "user-message-1"
    assert framework.stream_output_values == [True]
    assert framework.messages[0].content == "say hello"
    assert framework.messages[0].channel == "acp-server"
    assert framework.previous_routers[-1] is None

    update_names = [update.session_update for _, update in client.updates]
    assert update_names == [
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
    ]
    assert client.updates[0][1].content.text == "hello"
    assert client.updates[-1][1].content.text == " world"


@pytest.mark.asyncio
async def test_prompt_sends_complete_output_when_stream_has_no_text_chunks() -> None:
    framework = NoTextFramework()
    client = FakeClient()
    agent = BubACPAgent(framework)
    agent.on_connect(client)
    session = await agent.new_session(cwd=str(Path.cwd()))

    await agent.prompt([TextContentBlock(type="text", text="hello")], session_id=session.session_id)

    assert [update.content.text for _, update in client.updates] == ["late text"]
