from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import typer
from acp._transport import memory_transport_pair
from acp.exceptions import RequestError
from acp.experimental import v2
from acp.experimental.v2 import schema
from acp.http.client import create_http_stream
from acp.ws.client import create_websocket_stream
from bub import configure
from bub.core.tape import TapeEntry
from bub.model_selection import ModelChoice, ModelOptions
from bub.streaming import StreamEvent
from bub.turn import TurnResult
from bub_acp_server import plugin
from bub_acp_server.agent_v2 import BubACPV2Agent
from bub_acp_server.http import create_http_app
from bub_acp_server.mcp import server_configs_v2
from bub_acp_server.steering import ACPSteeringInbox
from hypercorn.asyncio import serve
from hypercorn.config import Config
from typer.testing import CliRunner

TIMEOUT = 5


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / ".bub"))
    monkeypatch.setitem(configure._global_config, "acp-server", [])
    monkeypatch.setitem(configure._config_data, "acp-server", {})


class Framework:
    """Streams one scripted turn; ``block`` holds turns open until released."""

    def __init__(self, workspace: Path, *, block: bool = False) -> None:
        self.workspace = workspace
        self.router: Any = None
        self.inbox = ACPSteeringInbox()
        self.block = block
        self.release = asyncio.Event()
        self.turn_started = asyncio.Event()
        self.prompts: list[str] = []
        self.injected: list[str] = []
        self.tape_entries: list[TapeEntry] = []

    def get_agent_hooks(self):
        return SimpleNamespace(subscribe=lambda events: events)

    def build_tape_context(self):
        return None

    def get_tape_sidecars(self):
        return ()

    def get_tools(self):
        return ()

    def bind_channel_router(self, router: Any) -> None:
        self.router = router

    def get_steering_inbox(self) -> ACPSteeringInbox:
        return self.inbox

    def get_tape_store(self):
        if not self.tape_entries:
            return None
        entries = self.tape_entries
        return SimpleNamespace(fetch_all=lambda query: entries)

    async def quit_via_channel_router(self, session_id: str) -> None:
        return None

    async def get_model_options(self, **kwargs: Any) -> ModelOptions:
        return ModelOptions(
            models=[ModelChoice(id="a", name="A"), ModelChoice(id="b", name="B")],
            current_model="a",
        )

    async def process_inbound(
        self, inbound: Any, stream_output: bool = False
    ) -> TurnResult:
        assert stream_output
        self.prompts.append(inbound.content)

        async def stream():
            yield StreamEvent("text", {"delta": "hello"})
            yield StreamEvent("reasoning", {"delta": "thinking"})
            yield StreamEvent(
                "tool_call",
                {
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {"command": "pwd"},
                        },
                        {"id": "call-2", "name": "tape.handoff", "arguments": {}},
                    ]
                },
            )
            if self.block:
                self.turn_started.set()
                await self.release.wait()
                state = {"session_id": inbound.session_id}
                for message in await self.inbox.drain_messages(state):
                    self.injected.append(message.content)
            yield StreamEvent("tool_result", {"tool_results": ["/workspace", "ok"]})
            yield StreamEvent("text", {"delta": " world"})
            yield StreamEvent(
                "usage",
                {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}},
            )

        async for _ in self.router.wrap_stream(inbound, stream()):
            pass
        return TurnResult(
            session_id=inbound.session_id,
            prompt=inbound.content,
            model_output="hello world",
        )


class Client:
    def __init__(self) -> None:
        self.updates: asyncio.Queue[Any] = asyncio.Queue()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.put_nowait(update)

    async def next(self) -> Any:
        return await asyncio.wait_for(self.updates.get(), TIMEOUT)

    async def until_idle(self) -> list[Any]:
        """Collect updates up to and including the end of a turn."""
        updates = []
        while True:
            update = await self.next()
            updates.append(update)
            if isinstance(update, schema.IdleSessionStateUpdate) and (
                update.stop_reason is not None
            ):
                return updates


@asynccontextmanager
async def connected(
    framework: Framework,
) -> AsyncIterator[tuple[v2.ClientSideConnection, Client, BubACPV2Agent]]:
    client_transport, agent_transport = memory_transport_pair()
    agent = BubACPV2Agent(framework, steering_inbox=framework.inbox)
    agent_connection = v2.AgentSideConnection(agent, agent_transport)
    client = Client()
    connection = v2.ClientSideConnection(client, client_transport)
    try:
        await connection.initialize(
            protocol_version=v2.PROTOCOL_VERSION,
            info=schema.Implementation(name="test", version="1.0.0"),
        )
        yield connection, client, agent
    finally:
        await agent.shutdown()
        await connection.close()
        await agent_connection.close()


async def new_session(
    connection: v2.ClientSideConnection, client: Client, cwd: Path
) -> str:
    session = await connection.new_session(cwd=str(cwd))
    assert isinstance(await client.next(), schema.IdleSessionStateUpdate)
    return session.session_id


async def wait_until_queued(inbox: ACPSteeringInbox, session_id: str) -> None:
    state = {"session_id": f"acp-server:{session_id}"}
    async with asyncio.timeout(TIMEOUT):
        while not inbox.message_count(state):
            await asyncio.sleep(0.01)


def text_prompt(text: str) -> list[schema.TextContentBlock]:
    return [schema.TextContentBlock(text=text)]


@pytest.mark.asyncio
async def test_initialize_advertises_v2_capabilities(tmp_path: Path) -> None:
    client_transport, agent_transport = memory_transport_pair()
    agent = BubACPV2Agent(Framework(tmp_path))
    agent_connection = v2.AgentSideConnection(agent, agent_transport)
    connection = v2.ClientSideConnection(Client(), client_transport)
    try:
        initialized = await connection.initialize(
            protocol_version=v2.PROTOCOL_VERSION,
            info=schema.Implementation(name="test", version="1.0.0"),
        )
    finally:
        await connection.close()
        await agent_connection.close()

    assert initialized.protocol_version == v2.PROTOCOL_VERSION
    assert initialized.info.name == "bub"
    session = initialized.capabilities.session
    assert session is not None
    assert session.prompt.image is not None
    assert session.mcp.stdio is not None and session.mcp.http is not None
    assert session.delete is not None
    assert session.additional_directories is not None


@pytest.mark.asyncio
async def test_prompt_returns_message_id_and_streams_turn(tmp_path: Path) -> None:
    async with connected(Framework(tmp_path)) as (connection, client, _):
        session_id = await new_session(connection, client, tmp_path)
        accepted = await connection.prompt(
            session_id=session_id, prompt=text_prompt("hi")
        )
        updates = await client.until_idle()

    echoed = next(u for u in updates if isinstance(u, schema.UserMessageUpdate))
    assert echoed.message_id == accepted.message_id
    assert echoed.content == text_prompt("hi")
    assert isinstance(updates[1], schema.RunningSessionStateUpdate)

    chunks = [u for u in updates if isinstance(u, schema.AgentMessageChunk)]
    assert [c.content.text for c in chunks] == ["hello", " world"]
    # Text after a tool call is a new agent message.
    assert chunks[0].message_id != chunks[1].message_id
    thought = next(u for u in updates if isinstance(u, schema.AgentThoughtChunk))
    assert thought.content.text == "thinking"

    tools = [u for u in updates if isinstance(u, schema.SessionToolCallUpdate)]
    assert [(t.tool_call_id, t.status) for t in tools] == [
        ("call-1", "in_progress"),
        ("call-1", "completed"),
    ]
    assert tools[0].title == "pwd" and tools[0].kind == "execute"
    assert tools[1].content[0].content.text == "/workspace"

    # Bub's tape handoff is a v2 context compaction, not a tool call.
    compactions = [u for u in updates if isinstance(u, schema.SessionCompactionUpdate)]
    assert [(c.compaction_id, c.status) for c in compactions] == [
        ("call-2", "in_progress"),
        ("call-2", "completed"),
    ]

    usage = next(u for u in updates if isinstance(u, schema.UsageUpdate))
    assert (usage.used, usage.size) == (15, 128_000)
    idle = updates[-1]
    assert idle.stop_reason == "end_turn"
    assert (idle.usage.input_tokens, idle.usage.output_tokens) == (10, 5)


@pytest.mark.asyncio
async def test_prompt_during_turn_is_injected(tmp_path: Path) -> None:
    framework = Framework(tmp_path, block=True)
    async with connected(framework) as (connection, client, _):
        session_id = await new_session(connection, client, tmp_path)
        await connection.prompt(session_id=session_id, prompt=text_prompt("first"))
        await asyncio.wait_for(framework.turn_started.wait(), TIMEOUT)

        # The second prompt is accepted at once, while the first turn still runs.
        accepted = await connection.prompt(
            session_id=session_id, prompt=text_prompt("steer")
        )
        await wait_until_queued(framework.inbox, session_id)
        framework.release.set()
        updates = await client.until_idle()

    assert framework.prompts == ["first"]
    assert framework.injected == ["steer"]
    echoed = [u for u in updates if isinstance(u, schema.UserMessageUpdate)]
    assert echoed[-1].message_id == accepted.message_id
    assert updates[-1].stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_prompt_after_final_drain_starts_new_turn(tmp_path: Path) -> None:
    framework = Framework(tmp_path, block=True)
    async with connected(framework) as (connection, client, _):
        session_id = await new_session(connection, client, tmp_path)
        await connection.prompt(session_id=session_id, prompt=text_prompt("first"))
        await asyncio.wait_for(framework.turn_started.wait(), TIMEOUT)
        await connection.prompt(session_id=session_id, prompt=text_prompt("next"))
        await wait_until_queued(framework.inbox, session_id)
        # The turn finishes without another model step, so it never drains "next".
        framework.inbox = ACPSteeringInbox()
        framework.release.set()
        await client.until_idle()
        await client.until_idle()

    assert framework.prompts == ["first", "next"]


@pytest.mark.asyncio
async def test_cancel_reports_cancelled_idle_state(tmp_path: Path) -> None:
    framework = Framework(tmp_path, block=True)
    async with connected(framework) as (connection, client, agent):
        session_id = await new_session(connection, client, tmp_path)
        await connection.prompt(session_id=session_id, prompt=text_prompt("hi"))
        await asyncio.wait_for(framework.turn_started.wait(), TIMEOUT)
        await connection.cancel_session(session_id=session_id)
        updates = await client.until_idle()
        assert not agent._prompt_runs

    assert updates[-1].stop_reason == "cancelled"


@pytest.mark.asyncio
async def test_resume_replays_history_from_start(tmp_path: Path) -> None:
    framework = Framework(tmp_path)
    async with connected(framework) as (connection, client, _):
        session_id = await new_session(connection, client, tmp_path)
        framework.tape_entries = [
            TapeEntry.message({"role": "user", "content": "question"}),
            TapeEntry.message({"role": "assistant", "content": "answer"}),
        ]
        await connection.resume_session(session_id=session_id, cwd=str(tmp_path))
        assert isinstance(await client.next(), schema.IdleSessionStateUpdate)

        await connection.resume_session(
            session_id=session_id,
            cwd=str(tmp_path),
            replay_from=schema.ReplayFromStartVariant(),
        )
        user = await client.next()
        assistant = await client.next()
        assert isinstance(await client.next(), schema.IdleSessionStateUpdate)

    assert isinstance(user, schema.UserMessageChunk)
    assert user.content.text == "question"
    assert isinstance(assistant, schema.AgentMessageChunk)
    assert assistant.content.text == "answer"


@pytest.mark.asyncio
async def test_sessions_list_close_and_delete(tmp_path: Path) -> None:
    async with connected(Framework(tmp_path)) as (connection, client, _):
        first = await new_session(connection, client, tmp_path)
        second = await new_session(connection, client, tmp_path)
        listed = await connection.list_sessions()
        assert {s.session_id for s in listed.sessions} == {first, second}
        assert listed.sessions[0].cwd == str(tmp_path.resolve())

        await connection.close_session(session_id=first)
        await connection.delete_session(session_id=second)
        assert (await connection.list_sessions()).sessions == []


@pytest.mark.asyncio
async def test_config_options_use_v2_shape(tmp_path: Path) -> None:
    async with connected(Framework(tmp_path)) as (connection, _, _):
        session = await connection.new_session(cwd=str(tmp_path))
        assert [o.config_id for o in session.config_options] == [
            "model",
            "reasoning_effort",
        ]
        updated = await connection.set_config_option(
            config_id="model", session_id=session.session_id, value="b"
        )
        assert updated.config_options[0].current_value == "b"
        with pytest.raises(RequestError):
            await connection.set_config_option(
                config_id="model", session_id=session.session_id, value="missing"
            )
        with pytest.raises(RequestError):
            await connection.set_config_option(
                config_id="model", session_id=session.session_id, value=True
            )


def test_v2_mcp_server_configs(tmp_path: Path) -> None:
    configs = server_configs_v2(
        [
            schema.StdioMcpServer(name="local", command="server"),
            schema.HttpMcpServer(
                name="remote",
                url="https://example.com/mcp",
                headers=[schema.HttpHeader(name="X-Key", value="secret")],
            ),
        ],
        tmp_path,
    )
    assert configs == {
        "local": {
            "transport": "stdio",
            "command": "server",
            "args": [],
            "env": {},
            "cwd": str(tmp_path),
        },
        "remote": {
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {"X-Key": "secret"},
        },
    }
    with pytest.raises(RequestError):
        server_configs_v2(
            [
                schema.StdioMcpServer(name="dup", command="a"),
                schema.StdioMcpServer(name="dup", command="b"),
            ],
            tmp_path,
        )
    with pytest.raises(RequestError):
        server_configs_v2(
            [schema.AcpMcpServer(name="acp", server_id="server-1")],
            tmp_path,
        )


@pytest.mark.parametrize(("args", "expected"), [([], "v1"), (["--v2"], "v2")])
def test_cli_selects_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], expected: str
) -> None:
    calls: list[str] = []

    async def run_v1(framework: Any) -> None:
        calls.append("v1")

    async def run_v2(framework: Any) -> None:
        calls.append("v2")

    monkeypatch.setattr(plugin, "run_acp_agent", run_v1)
    monkeypatch.setattr(plugin, "run_acp_v2_agent", run_v2)
    app = typer.Typer()

    @app.callback()
    def root() -> None:
        pass

    plugin.ACPServerPlugin(Framework(tmp_path)).register_cli_commands(app)
    result = CliRunner().invoke(app, ["acp", *args])
    assert result.exit_code == 0, result.output
    assert calls == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_web_transport_serves_v2(tmp_path: Path, transport: str) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = Config()
    config.bind = [f"fd://{listener.detach()}"]
    config.accesslog = None
    config.graceful_timeout = 1
    shutdown = asyncio.Event()
    server = asyncio.create_task(
        serve(
            create_http_app(Framework(tmp_path), v2=True),
            config,
            shutdown_trigger=shutdown.wait,
        )
    )
    url = f"http://127.0.0.1:{port}/acp"
    try:
        async with httpx.AsyncClient(trust_env=False) as http:
            async with asyncio.timeout(TIMEOUT):
                while True:
                    try:
                        await http.get(url.replace("/acp", "/missing"))
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.01)
        stream = (
            await create_websocket_stream(url.replace("http://", "ws://"))
            if transport == "websocket"
            else create_http_stream(url)
        )
        client = Client()
        connection = v2.connect_to_agent(client, stream)
        try:
            async with asyncio.timeout(TIMEOUT):
                await connection.initialize(
                    protocol_version=v2.PROTOCOL_VERSION,
                    info=schema.Implementation(name="test", version="1.0.0"),
                )
                session_id = await new_session(connection, client, tmp_path)
                accepted = await connection.prompt(
                    session_id=session_id, prompt=text_prompt("hi")
                )
                updates = await client.until_idle()
        finally:
            await connection.close()
    finally:
        shutdown.set()
        await asyncio.wait_for(server, TIMEOUT)

    echoed = next(u for u in updates if isinstance(u, schema.UserMessageUpdate))
    assert echoed.message_id == accepted.message_id
    assert updates[-1].stop_reason == "end_turn"
