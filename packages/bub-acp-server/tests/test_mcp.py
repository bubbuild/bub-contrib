from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from acp.exceptions import RequestError
from acp.schema import (
    EnvVariable,
    HttpHeader,
    HttpMcpServer,
    McpServerStdio,
    SseMcpServer,
    TextContentBlock,
)
from bub.builtin.agent import Agent
from bub.framework import BubFramework
from bub.tools import REGISTRY
from bub_mcp import plugin as mcp_plugin

from bub_acp_server.agent import BubACPAgent
from bub_acp_server.mcp import server_configs
from bub_acp_server.plugin import ACPServerPlugin


class Client:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))


class MCPClient:
    def __init__(self, config, *, init_timeout_seconds):
        self.config = config
        self.label = next(iter(config.values())).get("command", "remote")
        self.closed = False
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __aenter__(self):
        if self.label == "broken":
            raise RuntimeError("secret connection detail")
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def list_tools(self):
        return [
            SimpleNamespace(
                name="echo",
                description=self.label,
                inputSchema={"type": "object", "properties": {}},
            )
        ]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        self.started.set()
        if self.label == "blocked":
            await self.release.wait()
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.label)])


@pytest_asyncio.fixture
async def framework(tmp_path, monkeypatch):
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    framework.plugin_manager.register(ACPServerPlugin(framework), name="acp-server")
    async with framework.running():
        yield framework


@pytest.fixture
def clients(monkeypatch):
    clients = []

    def create(config, *, init_timeout_seconds):
        client = MCPClient(config, init_timeout_seconds=init_timeout_seconds)
        clients.append(client)
        return client

    monkeypatch.setattr(mcp_plugin, "_create_fastmcp_client", create)
    return clients


def stdio(label, name="shared"):
    return McpServerStdio(name=name, command=label, args=[], env=[])


def make_agent(framework, **kwargs):
    agent = BubACPAgent(framework, **kwargs)
    agent.on_connect(Client())
    return agent


async def call_echo(agent, session_id):
    return await agent.prompt(
        session_id=session_id, prompt=[TextContentBlock(text=",mcp.shared_echo")]
    )


async def runtime_tools(agent, session_id):
    inbound = agent._build_inbound([], agent._sessions[session_id])
    state = await agent.framework.build_state(inbound, inbound.session_id)
    return state["_runtime_agent"].tools


def test_transport_configs_preserve_arguments_environment_headers_and_cwd(tmp_path):
    configs = server_configs(
        [
            McpServerStdio(
                name="local",
                command="python",
                args=["server.py"],
                env=[EnvVariable(name="TOKEN", value="local-secret")],
            ),
            HttpMcpServer(
                type="http",
                name="http",
                url="https://example.test/mcp",
                headers=[HttpHeader(name="Authorization", value="http-secret")],
            ),
            SseMcpServer(
                type="sse",
                name="sse",
                url="https://example.test/sse",
                headers=[HttpHeader(name="X-Key", value="sse-secret")],
            ),
        ],
        tmp_path,
    )
    assert configs == {
        "local": {
            "transport": "stdio",
            "command": "python",
            "args": ["server.py"],
            "env": {"TOKEN": "local-secret"},
            "cwd": str(tmp_path),
        },
        "http": {
            "transport": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "http-secret"},
        },
        "sse": {
            "transport": "sse",
            "url": "https://example.test/sse",
            "headers": {"X-Key": "sse-secret"},
        },
    }


@pytest.mark.parametrize("names", [[""], ["same", " same "]])
def test_invalid_server_names_are_rejected(tmp_path, names):
    with pytest.raises(RequestError) as error:
        server_configs([stdio("ok", name) for name in names], tmp_path)
    assert error.value.code == -32602


@pytest.mark.asyncio
async def test_capabilities_and_session_tool_isolation(framework, clients, tmp_path):
    agent = make_agent(framework)
    caps = (await agent.initialize(1)).agent_capabilities.mcp_capabilities
    assert caps.http is True and caps.sse is True
    original = REGISTRY.copy()
    ordinary = Agent(framework)
    first = await agent.new_session(cwd=str(tmp_path), mcp_servers=[stdio("first")])
    second = await agent.new_session(cwd=str(tmp_path), mcp_servers=[stdio("second")])
    empty = await agent.new_session(cwd=str(tmp_path))
    try:
        for session in (first, second, first):
            await call_echo(agent, session.session_id)
        assert len(clients[0].calls) == 2
        assert len(clients[1].calls) == 1
        assert "mcp.shared_echo" not in await runtime_tools(agent, empty.session_id)
        assert "mcp.shared_echo" not in ordinary.tools
        assert REGISTRY == original
        await agent.close_session(first.session_id)
        assert clients[0].closed and not clients[1].closed
        await call_echo(agent, second.session_id)
    finally:
        await agent.shutdown()
    assert all(client.closed for client in clients)


@pytest.mark.asyncio
async def test_model_receives_only_current_session_mcp_tools(
    framework, clients, tmp_path, monkeypatch
):
    from bub.builtin.model_runner import ModelRunner
    from bub.streaming import AsyncStreamEvents, StreamEvent

    seen = []

    def run(self, *, tools, **kwargs):
        seen.append({tool.name for tool in tools})

        async def events():
            yield StreamEvent("text", {"delta": "ok"})
            yield StreamEvent("final", {"text": "ok", "ok": True})

        return AsyncStreamEvents(events())

    monkeypatch.setattr(ModelRunner, "run", run)
    agent = make_agent(framework)
    first = await agent.new_session(
        cwd=str(tmp_path), mcp_servers=[stdio("first", "first")]
    )
    second = await agent.new_session(
        cwd=str(tmp_path), mcp_servers=[stdio("second", "second")]
    )
    try:
        for session in (first, second):
            await agent.prompt(
                session_id=session.session_id, prompt=[TextContentBlock(text="hello")]
            )
        # Bub aliases dots for the model, but execution still uses each session's tool object.
        assert "mcp_first_echo" in seen[0] and "mcp_second_echo" not in seen[0]
        assert "mcp_second_echo" in seen[1] and "mcp_first_echo" not in seen[1]
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["load_session", "resume_session"])
@pytest.mark.parametrize("empty", [None, []])
async def test_reload_replaces_and_clears_config(
    framework, clients, tmp_path, method, empty
):
    agent = make_agent(framework)
    created = await agent.new_session(cwd=str(tmp_path), mcp_servers=[stdio("first")])
    session_id = created.session_id
    await call_echo(agent, session_id)
    try:
        await getattr(agent, method)(
            cwd=str(tmp_path), session_id=session_id, mcp_servers=[stdio("second")]
        )
        assert clients[0].closed
        await call_echo(agent, session_id)
        assert clients[1].calls == [("echo", {})]
        await getattr(agent, method)(
            cwd=str(tmp_path), session_id=session_id, mcp_servers=empty
        )
        assert clients[1].closed
        assert session_id not in agent._mcp_channels
        assert "mcp.shared_echo" not in await runtime_tools(agent, session_id)
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_failed_reload_keeps_previous_connection_and_closes_partial_success(
    framework, clients, tmp_path
):
    agent = make_agent(framework)
    created = await agent.new_session(
        cwd=str(tmp_path), mcp_servers=[stdio("original")]
    )
    try:
        with pytest.raises(RequestError) as error:
            await agent.resume_session(
                cwd=str(tmp_path),
                session_id=created.session_id,
                mcp_servers=[stdio("new", "new"), stdio("broken", "bad")],
            )
        assert "secret" not in str(error.value.to_error_obj())
        assert all(client.closed for client in clients[1:])
        assert not clients[0].closed
        await call_echo(agent, created.session_id)
        assert clients[0].calls == [("echo", {})]
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_failed_new_session_does_not_leave_metadata(framework, clients, tmp_path):
    agent = make_agent(framework)
    with pytest.raises(RequestError):
        await agent.new_session(cwd=str(tmp_path), mcp_servers=[stdio("broken")])
    assert agent._sessions == agent._mcp_channels == {}
    assert (await agent.list_sessions()).sessions == []
    assert clients[0].closed


@pytest.mark.asyncio
async def test_mcp_configuration_is_not_persisted(framework, clients, tmp_path):
    agent = make_agent(framework)
    server = HttpMcpServer(
        type="http",
        name="private",
        url="https://private.test/mcp",
        headers=[HttpHeader(name="Authorization", value="secret-token")],
    )
    session = await agent.new_session(cwd=str(tmp_path), mcp_servers=[server])
    try:
        stored = agent._session_store_path.read_text()
        assert all(
            value not in stored
            for value in ("private.test", "secret-token", "mcpServers")
        )
        assert not (tmp_path / "home" / "mcp.json").exists()
        fresh = make_agent(framework)
        await fresh.resume_session(cwd=str(tmp_path), session_id=session.session_id)
        assert fresh._mcp_channels == {}
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_close_cancels_active_tool_before_closing_client(
    framework, clients, tmp_path
):
    agent = make_agent(framework)
    session = await agent.new_session(cwd=str(tmp_path), mcp_servers=[stdio("blocked")])
    prompt = asyncio.create_task(call_echo(agent, session.session_id))
    try:
        async with asyncio.timeout(2):
            await clients[0].started.wait()
            await agent.close_session(session.session_id)
        assert prompt.cancelled()
        assert clients[0].closed
        assert agent._mcp_channels == {}
    finally:
        prompt.cancel()
        await asyncio.gather(prompt, return_exceptions=True)
        await agent.shutdown()


@pytest.mark.asyncio
async def test_reload_waits_for_active_tool(framework, clients, tmp_path):
    agent = make_agent(framework)
    session = await agent.new_session(cwd=str(tmp_path), mcp_servers=[stdio("blocked")])
    prompt = asyncio.create_task(call_echo(agent, session.session_id))
    reload = None
    try:
        async with asyncio.timeout(2):
            await clients[0].started.wait()
            reload = asyncio.create_task(
                agent.resume_session(
                    cwd=str(tmp_path),
                    session_id=session.session_id,
                    mcp_servers=[stdio("new")],
                )
            )
            await asyncio.sleep(0)
            assert not reload.done() and not clients[0].closed
            clients[0].release.set()
            await prompt
            await reload
        assert clients[0].closed
        await call_echo(agent, session.session_id)
        assert clients[1].calls == [("echo", {})]
    finally:
        prompt.cancel()
        if reload is not None:
            reload.cancel()
        await asyncio.gather(
            prompt, *([reload] if reload else []), return_exceptions=True
        )
        await agent.shutdown()


@pytest.mark.asyncio
async def test_real_stdio_server_runs_with_session_cwd_and_env(framework, tmp_path):
    script = Path(__file__).parent / "fixtures" / "mcp_tool_server.py"
    agent = make_agent(framework)
    session = await agent.new_session(
        cwd=str(tmp_path),
        mcp_servers=[
            McpServerStdio(
                name="local",
                command=sys.executable,
                args=[str(script.resolve())],
                env=[EnvVariable(name="ACP_MCP_TEST", value="session-env")],
            )
        ],
    )
    try:
        tools = await runtime_tools(agent, session.session_id)
        result = await tools["mcp.local_describe"].run()
        assert result == f"{tmp_path}:session-env"
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_http_agents_share_session_mcp_and_cleanup_before_shutdown_complete(
    framework, clients, tmp_path, monkeypatch
):
    from bub_acp_server import http

    factories = []

    def create_asgi_app(factory):
        factories.append(factory)

        async def app(scope, receive, send):
            await send({"type": "lifespan.shutdown.complete"})

        return app

    monkeypatch.setattr(http, "create_asgi_app", create_asgi_app)
    app = http.create_http_app(framework)
    first, second = [factories[0](Client()) for _ in range(2)]
    for agent in (first, second):
        agent.on_connect(Client())
    session = await first.new_session(cwd=str(tmp_path), mcp_servers=[stdio("first")])
    await call_echo(second, session.session_id)
    assert clients[0].calls == [("echo", {})]
    await second.load_session(
        cwd=str(tmp_path), session_id=session.session_id, mcp_servers=[stdio("second")]
    )
    assert clients[0].closed
    await call_echo(first, session.session_id)
    assert clients[1].calls == [("echo", {})]

    messages = []

    async def send(message):
        assert all(client.closed for client in clients)
        messages.append(message)

    await app({"type": "lifespan"}, None, send)
    assert messages == [{"type": "lifespan.shutdown.complete"}]
    assert first._mcp_channels == second._mcp_channels == {}


@pytest.mark.asyncio
async def test_session_tools_override_configured_mcp_and_restore_on_close(
    framework, clients, tmp_path
):
    configured = mcp_plugin.MCPPlugin(framework)
    framework.plugin_manager.register(configured, name="mcp")
    configured._manager._server_configs = {"shared": {"command": "configured"}}
    await configured._manager.connect()
    agent = make_agent(framework)
    session = await agent.new_session(cwd=str(tmp_path), mcp_servers=[stdio("session")])
    try:
        for _ in range(2):
            await call_echo(agent, session.session_id)
        assert clients[0].calls == []
        assert len(clients[1].calls) == 2
        runtime_agent = agent._runtime_agents[session.session_id]
        await agent.close_session(session.session_id)
        assert await runtime_agent.tools["mcp.shared_echo"].run() == "configured"
        await configured._manager.stop()
        assert "mcp.shared_echo" not in runtime_agent.tools
    finally:
        await agent.shutdown()
        await configured._manager.stop()


@pytest.mark.asyncio
async def test_stdio_agent_shutdown_closes_mcp_even_on_transport_error(
    framework, clients, tmp_path, monkeypatch
):
    from bub_acp_server import agent as module

    async def run_agent(agent, **kwargs):
        await agent.new_session(cwd=str(tmp_path), mcp_servers=[stdio("connected")])
        raise ConnectionError("ACP transport closed")

    monkeypatch.setattr(module, "run_agent", run_agent)
    with pytest.raises(ConnectionError):
        await module.run_acp_agent(framework)
    assert clients[0].closed


@pytest.mark.asyncio
async def test_shutdown_during_discovery_closes_partially_connected_clients(
    framework, clients, tmp_path, monkeypatch
):
    entered = asyncio.Event()
    original = MCPClient.list_tools

    async def list_tools(self):
        if self.label == "slow":
            entered.set()
            await asyncio.Event().wait()
        return await original(self)

    monkeypatch.setattr(MCPClient, "list_tools", list_tools)
    agent = make_agent(framework)
    setup = asyncio.create_task(
        agent.new_session(
            cwd=str(tmp_path),
            mcp_servers=[stdio("ready", "ready"), stdio("slow", "slow")],
        )
    )
    try:
        async with asyncio.timeout(2):
            await entered.wait()
            await agent.shutdown()
        with pytest.raises(asyncio.CancelledError):
            await setup
        assert len(clients) == 2 and all(client.closed for client in clients)
        assert agent._mcp_channels == {}
    finally:
        setup.cancel()
        await asyncio.gather(setup, return_exceptions=True)
