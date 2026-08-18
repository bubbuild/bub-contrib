from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
import typer
from acp.schema import TextContentBlock
from bub.model_selection import ModelChoice, ModelOptions
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.tape import (
    LAST_ANCHOR,
    InMemoryTapeStore,
    TapeContext,
    TapeEntry,
    TapeQuery,
    build_messages,
)
from bub.turn import TurnResult
from bub_acp_server import agent as agent_module
from bub_acp_server import plugin
from bub_acp_server.agent import ACPStreamRouter, BubACPAgent
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def isolated_bub_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / ".bub"))


class FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(
        self, session_id: str, update: object, **kwargs: Any
    ) -> None:
        self.updates.append((session_id, update))


class FakeFramework:
    def __init__(self) -> None:
        self.workspace = Path.cwd()
        self.router = None
        self.previous_routers: list[object] = []
        self.messages: list[object] = []
        self.stream_output_values: list[bool] = []
        self.workspaces_during_process: list[Path] = []

    def bind_channel_router(self, router: object) -> None:
        self.previous_routers.append(router)
        self.router = router

    async def quit_via_channel_router(self, session_id: str) -> None:
        return None

    async def get_model_options(
        self, *, session_id: str, workspace: Path
    ) -> ModelOptions:
        return ModelOptions()

    async def process_inbound(
        self, inbound: object, stream_output: bool = False
    ) -> TurnResult:
        self.messages.append(inbound)
        self.stream_output_values.append(stream_output)
        self.workspaces_during_process.append(self.workspace)

        async def stream():
            yield StreamEvent("text", {"delta": "hello"})
            yield StreamEvent("reasoning", {"delta": "thinking hard"})
            yield StreamEvent(
                "tool_call",
                {
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": '{"cmd":"pwd"}',
                            },
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {
                                "name": "fs.read",
                                "arguments": '{"path":"README.md"}',
                            },
                        },
                    ]
                },
            )
            yield StreamEvent(
                "tool_result", {"tool_results": ["/workspace", "README content"]}
            )
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
    async def process_inbound(
        self, inbound: object, stream_output: bool = False
    ) -> TurnResult:
        self.messages.append(inbound)
        self.stream_output_values.append(stream_output)

        async def stream():
            yield StreamEvent("final", {"text": "late text", "ok": True})

        async for _ in self.router.wrap_stream(inbound, stream()):
            pass
        return TurnResult(
            session_id=inbound.session_id,
            prompt=inbound.content,
            model_output="late text",
        )


class ConfigFramework(FakeFramework):
    def __init__(self) -> None:
        super().__init__()
        self.model_queries: list[tuple[str, Path]] = []

    async def get_model_options(
        self, *, session_id: str, workspace: Path
    ) -> ModelOptions:
        self.model_queries.append((session_id, workspace))
        return ModelOptions(
            models=[
                ModelChoice(
                    id="openai:gpt-5",
                    name="GPT-5",
                    description="OpenAI model",
                ),
                ModelChoice(
                    id="anthropic:claude-sonnet-4-5",
                    name="Claude Sonnet",
                ),
            ],
            current_model="openai:gpt-5",
        )


@pytest.mark.parametrize(
    ("arguments", "exit_code", "message", "expected_calls"),
    [
        (["acp"], 0, "", 1),
        (["acp", "serve"], 0, "is deprecated", 1),
        (["acp", "invalid"], 2, "Got unexpected extra argument", 0),
    ],
)
def test_register_cli_accepts_only_deprecated_serve_argument(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    exit_code: int,
    message: str,
    expected_calls: int,
) -> None:
    framework = FakeFramework()
    calls: list[FakeFramework] = []

    async def fake_run_acp_agent(candidate: FakeFramework) -> None:
        calls.append(candidate)

    monkeypatch.setattr(plugin, "run_acp_agent", fake_run_acp_agent)
    app = typer.Typer()

    @app.callback()
    def main() -> None:
        pass

    plugin.ACPServerPlugin(cast(Any, framework)).register_cli_commands(app)

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == exit_code
    assert message in result.output
    assert calls == [framework] * expected_calls


def test_plan_prompt_is_enabled_for_acp_server_turns() -> None:
    implementation = plugin.ACPServerPlugin(cast(Any, FakeFramework()))

    acp_prompt = implementation.system_prompt(
        "task", {"context": "channel=$acp-server|chat_id=session-1"}
    )

    assert "`update_plan` tool" in acp_prompt
    assert "complete plan on every update" in acp_prompt
    assert "latest persisted plan" in acp_prompt


@pytest.mark.asyncio
async def test_tape_context_injects_latest_plan_after_last_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_plan = TapeEntry.event(
        "plan",
        {"entries": [{"content": "Old step", "status": "in_progress"}]},
    )
    latest_plan = TapeEntry.event(
        "plan",
        {
            "entries": [
                {
                    "content": "Current step",
                    "priority": "high",
                    "status": "in_progress",
                }
            ],
            "explanation": "Current direction",
        },
    )
    entries = [
        TapeEntry.message({"role": "user", "content": "Before anchor"}),
        old_plan,
        TapeEntry.anchor("handoff"),
        TapeEntry.message({"role": "user", "content": "After anchor"}),
        latest_plan,
    ]
    implementation = plugin.ACPServerPlugin(cast(Any, FakeFramework()))
    default_select = plugin.default_tape_context().select
    assert default_select is not None

    async def async_default_select(
        selected_entries: object, context: TapeContext
    ) -> list[dict[str, Any]]:
        return cast(Any, default_select)(selected_entries, context)

    monkeypatch.setattr(
        plugin,
        "default_tape_context",
        lambda: TapeContext(select=async_default_select),
    )
    context = implementation.build_tape_context()
    context.state["context"] = "channel=$acp-server|chat_id=session-1"
    store = InMemoryTapeStore()
    for entry in entries:
        store.append("session", entry)

    contextual_entries = context.build_query(TapeQuery("session", store)).all()
    messages = await cast(Any, build_messages(contextual_entries, context))

    assert context.anchor is LAST_ANCHOR
    assert messages[0] == {"role": "user", "content": "After anchor"}
    assert messages[1]["role"] == "assistant"
    assert "<current_plan>" in messages[1]["content"]
    assert '"content": "Current step"' in messages[1]["content"]
    assert '"explanation": "Current direction"' in messages[1]["content"]
    assert all("Before anchor" not in message["content"] for message in messages)
    assert all("Old step" not in message["content"] for message in messages)


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
    assert response.field_meta == {"steering": {"supported": True}}


@pytest.mark.asyncio
async def test_resume_adopts_existing_editor_session_ids(tmp_path: Path) -> None:
    agent = BubACPAgent(FakeFramework())

    resume_response = await agent.resume_session(
        cwd=str(tmp_path), session_id="zed-session"
    )
    sessions = await agent.list_sessions(cwd=str(tmp_path))

    assert resume_response is not None
    assert [session.session_id for session in sessions.sessions] == ["zed-session"]
    assert sessions.sessions[0].cwd == str(tmp_path)


@pytest.mark.asyncio
async def test_load_session_attaches_tape_history_through_streaming_router(
    tmp_path: Path,
) -> None:
    session_id = "zed-session"
    entries = [
        TapeEntry(
            1,
            "message",
            {
                "role": "user",
                "content": (
                    f"channel=$acp-server|chat_id={session_id}\n"
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
                "content": "Continue the task until all targets are completed. [context: chat_id=x]",
            },
        ),
    ]
    framework = TapeFramework(entries)
    client = FakeClient()
    agent = BubACPAgent(framework)
    agent.on_connect(client)

    response = await agent.load_session(cwd=str(tmp_path), session_id=session_id)

    assert response is not None
    assert framework.tape_store.queries == [
        agent_module._session_tape_name(
            agent_module._bub_session_id("acp-server", session_id), tmp_path
        )
    ]
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
async def test_session_lifecycle_returns_config_options(tmp_path: Path) -> None:
    framework = ConfigFramework()
    client = FakeClient()
    agent = BubACPAgent(framework)
    agent.on_connect(client)

    created = await agent.new_session(cwd=str(tmp_path))
    loaded = await agent.load_session(cwd=str(tmp_path), session_id=created.session_id)
    resumed = await agent.resume_session(
        cwd=str(tmp_path), session_id=created.session_id
    )

    assert created.config_options is not None
    assert created.config_options[0].id == "model"
    assert created.config_options[0].name == "Model"
    assert created.config_options[0].current_value == "openai:gpt-5"
    assert created.config_options[0].options[0].value == "openai:gpt-5"
    assert created.config_options[1].id == "reasoning_effort"
    assert created.config_options[1].name == "Reasoning effort"
    assert created.config_options[1].category == "thought_level"
    assert created.config_options[1].current_value == "auto"
    assert [option.value for option in created.config_options[1].options] == [
        "auto",
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ]
    assert len(created.config_options) == 2
    assert loaded.config_options is not None
    assert [option.id for option in loaded.config_options] == [
        "model",
        "reasoning_effort",
    ]
    assert resumed.config_options is not None
    assert [option.id for option in resumed.config_options] == [
        "model",
        "reasoning_effort",
    ]
    assert framework.model_queries == [
        (f"acp-server:{created.session_id}", tmp_path),
        (f"acp-server:{created.session_id}", tmp_path),
        (f"acp-server:{created.session_id}", tmp_path),
    ]


@pytest.mark.asyncio
async def test_set_reasoning_effort_updates_session_runtime_and_config_option(
    tmp_path: Path,
) -> None:
    framework = ConfigFramework()
    agent = BubACPAgent(framework)
    created = await agent.new_session(cwd=str(tmp_path))

    response = await agent.set_config_option(
        config_id="reasoning_effort",
        session_id=created.session_id,
        value="high",
    )

    assert agent._sessions[created.session_id].runtime == {
        "reasoning_effort": "high"
    }
    reasoning_option = next(
        option
        for option in response.config_options
        if option.id == "reasoning_effort"
    )
    assert reasoning_option.current_value == "high"


@pytest.mark.asyncio
async def test_set_reasoning_effort_rejects_unsupported_value(tmp_path: Path) -> None:
    agent = BubACPAgent(ConfigFramework())
    created = await agent.new_session(cwd=str(tmp_path))

    with pytest.raises(ValueError, match="invalid value"):
        await agent.set_config_option(
            config_id="reasoning_effort",
            session_id=created.session_id,
            value="extreme",
        )


@pytest.mark.asyncio
async def test_set_config_option_updates_session_runtime_and_returns_config_options(
    tmp_path: Path,
) -> None:
    framework = ConfigFramework()
    agent = BubACPAgent(framework)
    created = await agent.new_session(cwd=str(tmp_path))

    response = await agent.set_config_option(
        config_id="model",
        session_id=created.session_id,
        value="anthropic:claude-sonnet-4-5",
    )

    assert agent._sessions[created.session_id].runtime == {
        "model": "anthropic:claude-sonnet-4-5"
    }
    assert response.config_options[0].id == "model"
    assert response.config_options[0].current_value == "anthropic:claude-sonnet-4-5"
    assert framework.model_queries == [
        (f"acp-server:{created.session_id}", tmp_path),
        (f"acp-server:{created.session_id}", tmp_path),
    ]


def test_model_options_fall_back_when_persisted_model_is_unavailable(
    tmp_path: Path,
) -> None:
    session = agent_module.ACPSession(
        session_id="session",
        cwd=tmp_path,
        runtime={"model": "removed:model"},
    )
    options = ModelOptions(
        models=[ModelChoice(id="available:model")],
        current_model="available:model",
    )

    config_options = agent_module._model_options_to_acp_config_options(options, session)

    assert config_options[0].current_value == "available:model"


@pytest.mark.asyncio
async def test_prompt_passes_session_config_to_bub_context(tmp_path: Path) -> None:
    framework = ConfigFramework()
    framework_workspace = framework.workspace
    client = FakeClient()
    agent = BubACPAgent(framework)
    agent.on_connect(client)
    created = await agent.new_session(cwd=str(tmp_path))
    await agent.set_config_option(
        config_id="model",
        session_id=created.session_id,
        value="anthropic:claude-sonnet-4-5",
    )
    await agent.set_config_option(
        config_id="reasoning_effort",
        session_id=created.session_id,
        value="high",
    )

    await agent.prompt(
        [TextContentBlock(type="text", text="hello")],
        session_id=created.session_id,
    )

    assert framework.messages[0].context["_runtime_model"] == "anthropic:claude-sonnet-4-5"
    assert framework.messages[0].context["_runtime_reasoning_effort"] == "high"
    assert framework.messages[0].context["_runtime_workspace"] == str(tmp_path)
    assert framework.messages[0].context["chat_id"] == created.session_id
    assert "acp_session_id" not in framework.messages[0].context
    assert framework.messages[0].session_id == f"acp-server:{created.session_id}"
    assert not hasattr(framework.messages[0], "runtime")
    assert framework.workspaces_during_process == [framework_workspace]
    assert framework.workspace == framework_workspace


@pytest.mark.asyncio
async def test_session_store_expands_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BUB_HOME", "~/.custom-bub")

    agent = BubACPAgent(FakeFramework())
    await agent.new_session(cwd=str(tmp_path))

    assert (tmp_path / ".custom-bub" / "acp-sessions.json").exists()


@pytest.mark.asyncio
async def test_run_acp_agent_registers_resume_routes_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class RunningFramework(FakeFramework):
        def running(self):
            class Context:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *args: object) -> None:
                    return None

            return Context()

    async def fake_run_agent(
        agent: object, *, use_unstable_protocol: bool = False
    ) -> None:
        captured["agent"] = agent
        captured["use_unstable_protocol"] = use_unstable_protocol

    monkeypatch.setattr(agent_module, "run_agent", fake_run_agent)

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
    )

    assert response.stop_reason == "end_turn"
    assert framework.stream_output_values == [True]
    assert framework.messages[0].content == "say hello"
    assert framework.messages[0].channel == "acp-server"
    assert framework.previous_routers == [framework.router]

    update_names = [update.session_update for _, update in client.updates]
    assert update_names == [
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call",
        "tool_call_update",
        "tool_call_update",
        "agent_message_chunk",
    ]
    assert client.updates[0][1].content.text == "hello"
    thought = client.updates[1][1]
    assert thought.content.text == "thinking hard"
    first_call = client.updates[2][1]
    second_call = client.updates[3][1]
    first_result = client.updates[4][1]
    second_result = client.updates[5][1]
    assert first_call.tool_call_id == "call-1"
    assert first_call.title == "pwd"
    assert first_call.kind == "execute"
    assert first_call.raw_input == {"cmd": "pwd"}
    assert second_call.tool_call_id == "call-2"
    assert second_call.title == "fs.read"
    assert second_call.kind == "read"
    assert first_result.tool_call_id == "call-1"
    assert first_result.raw_output == "/workspace"
    assert first_result.content[0].content.text == "/workspace"
    assert second_result.tool_call_id == "call-2"
    assert second_result.raw_output == "README content"
    assert client.updates[-1][1].content.text == " world"


@pytest.mark.asyncio
async def test_bash_tool_call_attaches_acp_terminal_content() -> None:
    client = FakeClient()
    router = ACPStreamRouter(client)

    async def stream():
        yield StreamEvent(
            "tool_call",
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"cmd":"pwd"}',
                        },
                    }
                ]
            },
        )
        await router.attach_terminal("session-1", "pwd", "terminal-1")
        yield StreamEvent("tool_result", {"tool_results": ["/workspace"]})

    async for _ in router.wrap_stream({"chat_id": "session-1"}, stream()):
        pass

    start = client.updates[0][1]
    terminal_update = client.updates[1][1]
    result_update = client.updates[2][1]
    assert start.title == "pwd"
    assert start.raw_input == {"cmd": "pwd"}
    assert terminal_update.tool_call_id == "call-1"
    assert terminal_update.status == "in_progress"
    assert terminal_update.content[0].type == "terminal"
    assert terminal_update.content[0].terminal_id == "terminal-1"
    assert result_update.tool_call_id == "call-1"
    assert result_update.status == "completed"
    assert result_update.raw_output == "/workspace"
    assert result_update.content is None


@pytest.mark.asyncio
async def test_tape_handoff_is_reported_as_context_compaction() -> None:
    client = FakeClient()
    router = ACPStreamRouter(client)

    async def stream():
        yield StreamEvent(
            "tool_call",
            {
                "tool_calls": [
                    {
                        "id": "call-handoff",
                        "type": "function",
                        "function": {
                            "name": "tape.handoff",
                            "arguments": '{"name":"phase-1","summary":"done"}',
                        },
                    }
                ]
            },
        )
        yield StreamEvent(
            "tool_result", {"tool_results": ["anchor added: phase-1"]}
        )

    async for _ in router.wrap_stream({"chat_id": "session-1"}, stream()):
        pass

    start = client.updates[0][1]
    completed = client.updates[1][1]
    assert start.session_update == "tool_call"
    assert start.tool_call_id == "call-handoff"
    assert start.title == "Context compacting"
    assert start.kind == "other"
    assert start.status == "in_progress"
    assert start.field_meta == {"contextCompaction": True}
    assert completed.session_update == "tool_call_update"
    assert completed.tool_call_id == "call-handoff"
    assert completed.title == "Context compacted"
    assert completed.status == "completed"
    assert completed.content is None
    assert completed.field_meta == {"contextCompaction": True}


@pytest.mark.asyncio
async def test_stream_router_isolates_concurrent_session_state() -> None:
    client = FakeClient()
    router = ACPStreamRouter(client)
    both_started = asyncio.Event()
    started = 0

    async def stream(tool_call_id: str):
        nonlocal started
        yield StreamEvent(
            "tool_call",
            {
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {"name": "fs.read", "arguments": "{}"},
                    }
                ]
            },
        )
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        yield StreamEvent("tool_result", {"tool_results": [tool_call_id]})

    async def consume(session_id: str, tool_call_id: str) -> None:
        async for _ in router.wrap_stream(
            {"chat_id": session_id}, stream(tool_call_id)
        ):
            pass

    await asyncio.gather(
        consume("session-1", "call-1"),
        consume("session-2", "call-2"),
    )

    completed_calls = {
        session_id: update.tool_call_id
        for session_id, update in client.updates
        if update.session_update == "tool_call_update"
    }
    assert completed_calls == {"session-1": "call-1", "session-2": "call-2"}


@pytest.mark.asyncio
async def test_stream_router_reports_usage_before_turn_finishes() -> None:
    client = FakeClient()
    router = ACPStreamRouter(client, context_window_size=100)
    stream_state = StreamState()
    release_turn = asyncio.Event()

    async def stream():
        yield StreamEvent("text", {"delta": "first"})
        stream_state.usage = {"prompt_tokens": 10, "completion_tokens": 1}
        yield StreamEvent("reasoning", {"delta": "working"})
        await release_turn.wait()
        stream_state.usage = {"prompt_tokens": 10, "completion_tokens": 2}
        yield StreamEvent("text", {"delta": "second"})

    events = AsyncStreamEvents(stream(), state=stream_state)

    async def consume() -> None:
        async for _ in router.wrap_stream({"chat_id": "session-1"}, events):
            pass

    task = asyncio.create_task(consume())
    async with asyncio.timeout(1):
        while not any(
            update.session_update == "usage_update" for _, update in client.updates
        ):
            await asyncio.sleep(0)

    first_usage = next(
        update
        for _, update in client.updates
        if update.session_update == "usage_update"
    )
    assert first_usage.used == 11
    assert first_usage.size == 100
    assert not task.done()

    release_turn.set()
    await task

    usage_updates = [
        update
        for _, update in client.updates
        if update.session_update == "usage_update"
    ]
    assert [(update.used, update.size) for update in usage_updates] == [
        (11, 100),
        (12, 100),
    ]


@pytest.mark.asyncio
async def test_prompt_sends_complete_output_when_stream_has_no_text_chunks() -> None:
    framework = NoTextFramework()
    client = FakeClient()
    agent = BubACPAgent(framework)
    agent.on_connect(client)
    session = await agent.new_session(cwd=str(Path.cwd()))

    await agent.prompt(
        [TextContentBlock(type="text", text="hello")], session_id=session.session_id
    )

    assert client.updates[0][1].content.text == "late text"
    assert len(client.updates) == 1
