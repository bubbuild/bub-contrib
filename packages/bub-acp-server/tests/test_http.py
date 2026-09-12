from __future__ import annotations

import asyncio
import json
import socket
import ssl
import subprocess
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from acp import connect_to_agent
from acp.exceptions import RequestError
from acp.http import create_http_stream
from acp.ws import create_websocket_stream
from acp.schema import (
    ClientCapabilities,
    CreateTerminalResponse,
    TerminalOutputResponse,
    TextContentBlock,
    WaitForTerminalExitResponse,
)
from bub.model_selection import ModelOptions
from bub.framework import BubFramework
from bub import configure
from bub.channels import Channel, Interface
from bub.channels.manager import ChannelManager
from bub.channels.message import ChannelMessage
from bub.streaming import StreamEvent
from bub.tape import (
    AsyncTapeStoreAdapter,
    InMemoryTapeStore,
    Tape,
    TapeContext,
    TapeEntry,
)
from bub.tools import REGISTRY, ToolContext
from bub.turn import TurnResult
from hypercorn.asyncio import serve
from hypercorn.config import Config
from typer.testing import CliRunner

from bub_acp_server import http as http_module
from bub_acp_server.agent import BubACPAgent
from bub_acp_server.plugin import ACPServerPlugin
from bub_acp_server.http import ACPHTTPChannel, create_http_app
from bub_acp_server.config import ACPServerSettings
from bub_acp_server.steering import ACPSteeringInbox


@pytest.fixture(autouse=True)
def isolated_acp_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(configure._global_config, "acp-server", [])
    monkeypatch.setitem(configure._config_data, "acp-server", {})


class HTTPFramework:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.router: Any = None
        self.inbox = ACPSteeringInbox()

    def bind_channel_router(self, router: Any) -> None:
        self.router = router

    def get_steering_inbox(self) -> ACPSteeringInbox:
        return self.inbox

    def get_tape_store(self):
        return None

    async def get_model_options(self, **kwargs: Any) -> ModelOptions:
        return ModelOptions()

    async def process_inbound(
        self, inbound: Any, stream_output: bool = False
    ) -> TurnResult:
        assert stream_output
        output = ""

        async def stream():
            nonlocal output
            yield StreamEvent(
                "tool_call",
                {
                    "tool_calls": [
                        {
                            "id": "bash-1",
                            "name": "bash",
                            "arguments": {
                                "cmd": inbound.content,
                                "title": "Run command",
                            },
                        }
                    ]
                },
            )
            output = await REGISTRY["bash"].run(
                cmd=inbound.content,
                context=ToolContext(
                    tape=None,
                    state={
                        "session_id": inbound.session_id,
                        "_runtime_workspace": str(self.workspace),
                    },
                ),
            )
            yield StreamEvent("tool_result", {"tool_results": [output]})
            yield StreamEvent("text", {"delta": output})

        async for _ in self.router.wrap_stream(inbound, stream()):
            pass
        return TurnResult(
            session_id=inbound.session_id, prompt=inbound.content, model_output=output
        )


class HTTPClient:
    def __init__(self, label: str) -> None:
        self.label = label
        self.commands: list[dict[str, Any]] = []
        self.updates: list[tuple[str, Any]] = []
        self.command_started = asyncio.Event()
        self.release_command = asyncio.Event()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))

    async def create_terminal(self, **kwargs: Any) -> CreateTerminalResponse:
        self.commands.append(kwargs)
        self.command_started.set()
        await self.release_command.wait()
        return CreateTerminalResponse(terminal_id=self.label)

    async def wait_for_terminal_exit(
        self, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        return WaitForTerminalExitResponse(exit_code=0)

    async def terminal_output(self, **kwargs: Any) -> TerminalOutputResponse:
        return TerminalOutputResponse(output=self.label, truncated=False)

    async def release_terminal(self, **kwargs: Any) -> None:
        pass


@asynccontextmanager
async def http_server(tmp_path: Path, *, tls: bool, framework=None):
    config = Config()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config.bind = [f"fd://{listener.detach()}"]
    config.accesslog = None
    config.graceful_timeout = 1
    if tls:
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )
        config.certfile, config.keyfile = str(cert), str(key)
    shutdown = asyncio.Event()
    task = asyncio.create_task(
        serve(
            create_http_app(framework or HTTPFramework(tmp_path)),
            config,
            shutdown_trigger=shutdown.wait,
        )
    )
    url = f"{'https' if tls else 'http'}://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(
            verify=False, http2=tls, trust_env=False
        ) as client:
            async with asyncio.timeout(5):
                while True:
                    try:
                        response = await client.get(url + "/missing")
                        assert response.status_code == 404
                        assert response.http_version == (
                            "HTTP/2" if tls else "HTTP/1.1"
                        )
                        break
                    except httpx.ConnectError:
                        if task.done():
                            await task
                        await asyncio.sleep(0.01)
        yield url + "/acp"
    finally:
        shutdown.set()
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("tls", [False, True])
async def test_http_connections_keep_tools_and_streams_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tls: bool
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    from bub.builtin import tools as builtin_tools  # noqa: F401

    original_bash = REGISTRY["bash"]
    first, second = HTTPClient("first"), HTTPClient("second")
    second.release_command.set()
    async with http_server(tmp_path, tls=tls) as url:
        async with (
            httpx.AsyncClient(
                verify=False, http2=tls, timeout=None, trust_env=False
            ) as http_one,
            httpx.AsyncClient(
                verify=False, http2=tls, timeout=None, trust_env=False
            ) as http_two,
        ):
            one = connect_to_agent(first, create_http_stream(url, client=http_one))
            two = connect_to_agent(second, create_http_stream(url, client=http_two))
            try:
                async with asyncio.timeout(10):
                    initialized = await one.initialize(
                        protocol_version=1,
                        client_capabilities=ClientCapabilities(terminal=True),
                    )
                    assert initialized.agent_capabilities.load_session is True
                    assert (
                        initialized.agent_capabilities.session_capabilities.resume
                        is None
                    )
                    session1 = await one.new_session(cwd=str(tmp_path))
                    assert (
                        initialized.agent_capabilities.session_capabilities.close
                        is None
                    )
                    await one.load_session(
                        cwd=str(tmp_path), session_id=session1.session_id
                    )
                    first_prompt = asyncio.create_task(
                        one.prompt(
                            session1.session_id,
                            [TextContentBlock(type="text", text="pwd")],
                        )
                    )
                    await first.command_started.wait()
                    # Connecting a second client during an active tool must not rebind its runtime.
                    await two.initialize(
                        protocol_version=1,
                        client_capabilities=ClientCapabilities(terminal=True),
                    )
                    session2 = await two.new_session(cwd=str(tmp_path))
                    second_prompt = asyncio.create_task(
                        two.prompt(
                            session2.session_id,
                            [TextContentBlock(type="text", text="ls")],
                        )
                    )
                    first.release_command.set()
                    responses = await asyncio.gather(first_prompt, second_prompt)
                    assert all(
                        response.stop_reason == "end_turn" for response in responses
                    )
                    listed = await one.list_sessions()
                    assert {session.session_id for session in listed.sessions} == {
                        session1.session_id,
                        session2.session_id,
                    }
                    for client, session, cmd in [
                        (first, session1, "pwd"),
                        (second, session2, "ls"),
                    ]:
                        assert client.commands[0]["args"] == ["-lc", cmd]
                        assert client.commands[0]["session_id"] == session.session_id
                        assert {session_id for session_id, _ in client.updates} == {
                            session.session_id
                        }
                        text = [
                            update.content.text
                            for _, update in client.updates
                            if update.session_update == "agent_message_chunk"
                        ]
                        assert text == [client.label]
            finally:
                first.release_command.set()
                await one.close()
                await two.close()
    assert REGISTRY["bash"] is original_bash
    assert len(json.loads((tmp_path / "home" / "acp-sessions.json").read_text())) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tls", [False, True])
@pytest.mark.parametrize("history_size", [0, 1100])
async def test_http_load_persisted_session_on_fresh_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tls: bool, history_size: int
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    from bub.builtin import tools as builtin_tools  # noqa: F401

    # Persist metadata before starting the HTTP server; its fresh agent must
    # restore the session without a session/new request on this connection.
    session = await BubACPAgent(HTTPFramework(tmp_path)).new_session(cwd=str(tmp_path))
    store = AsyncTapeStoreAdapter(InMemoryTapeStore())
    tape = Tape(tmp_path, store, TapeContext()).session_tape(
        f"acp-server:{session.session_id}", tmp_path
    )
    for index in range(history_size):
        await store.append(
            tape.name,
            TapeEntry.message({"role": "assistant", "content": f"history-{index}"}),
        )
    framework = HTTPFramework(tmp_path)
    monkeypatch.setattr(framework, "get_tape_store", lambda: store)
    client = HTTPClient("continued")
    client.release_command.set()
    async with http_server(tmp_path, tls=tls, framework=framework) as url:
        async with httpx.AsyncClient(
            verify=False, http2=tls, timeout=None, trust_env=False
        ) as http:
            connection = connect_to_agent(client, create_http_stream(url, client=http))
            try:
                async with asyncio.timeout(15):
                    await connection.initialize(
                        protocol_version=1,
                        client_capabilities=ClientCapabilities(terminal=True),
                    )
                    assert (await connection.list_sessions()).sessions[
                        0
                    ].session_id == session.session_id
                    loaded = await connection.load_session(
                        cwd=str(tmp_path), session_id=session.session_id
                    )
                    assert loaded.config_options
                    # The SDK dispatches notification callbacks asynchronously.
                    while len(client.updates) < history_size:
                        await asyncio.sleep(0)
                    assert [u.content.text for _, u in client.updates] == [
                        f"history-{i}" for i in range(history_size)
                    ]
                    result = await connection.prompt(
                        session.session_id, [TextContentBlock(text="pwd")]
                    )
                    assert result.stop_reason == "end_turn"
                    while not any(
                        u.session_update == "agent_message_chunk"
                        and u.content.text == "continued"
                        for _, u in client.updates
                    ):
                        await asyncio.sleep(0)
                    assert {sid for sid, _ in client.updates} == {session.session_id}
                    assert client.commands[0]["session_id"] == session.session_id
                    await connection.load_session(
                        cwd=str(tmp_path), session_id=session.session_id
                    )
            finally:
                await connection.close()


@pytest.mark.asyncio
async def test_http_load_failure_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    from bub.builtin import tools as builtin_tools  # noqa: F401

    framework = HTTPFramework(tmp_path)

    def fail_store():
        raise OSError("store unavailable")

    monkeypatch.setattr(framework, "get_tape_store", fail_store)
    client = HTTPClient("retried")
    client.release_command.set()
    async with http_server(tmp_path, tls=False, framework=framework) as url:
        async with httpx.AsyncClient(timeout=None, trust_env=False) as http:
            connection = connect_to_agent(client, create_http_stream(url, client=http))
            try:
                async with asyncio.timeout(10):
                    await connection.initialize(
                        protocol_version=1,
                        client_capabilities=ClientCapabilities(terminal=True),
                    )
                    with pytest.raises(RequestError):
                        await connection.load_session(
                            cwd=str(tmp_path), session_id="history"
                        )
                    monkeypatch.setattr(framework, "get_tape_store", lambda: None)
                    await connection.load_session(
                        cwd=str(tmp_path), session_id="history"
                    )
                    assert (
                        await connection.prompt(
                            "history", [TextContentBlock(text="pwd")]
                        )
                    ).stop_reason == "end_turn"
                    monkeypatch.setattr(framework, "get_tape_store", fail_store)
                    with pytest.raises(RequestError):
                        await connection.load_session(
                            cwd=str(tmp_path), session_id="history"
                        )
                    assert (
                        await connection.prompt(
                            "history", [TextContentBlock(text="pwd")]
                        )
                    ).stop_reason == "end_turn"
            finally:
                await connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tls", [False, True])
async def test_websocket_load_and_tool_callbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tls: bool
) -> None:
    from acp.ws import client as ws_client
    from bub.builtin import tools as builtin_tools  # noqa: F401

    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    framework = HTTPFramework(tmp_path)
    store = AsyncTapeStoreAdapter(InMemoryTapeStore())
    monkeypatch.setattr(framework, "get_tape_store", lambda: store)
    async with http_server(tmp_path, tls=tls, framework=framework) as url:
        # Trust only this test server's self-signed certificate. The SDK's
        # convenience client does not expose an SSL-context argument.
        connect_options = {"proxy": None}
        if tls:
            context = ssl.create_default_context(cafile=str(tmp_path / "cert.pem"))
            context.check_hostname = False  # Test certificate is for localhost.
            connect_options["ssl"] = context
        monkeypatch.setattr(
            ws_client, "ws_connect", partial(ws_client.ws_connect, **connect_options)
        )
        ws_url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        client = HTTPClient("websocket output")
        client.release_command.set()
        connection = connect_to_agent(client, await create_websocket_stream(ws_url))
        try:
            async with asyncio.timeout(10):
                initialized = await connection.initialize(
                    protocol_version=1,
                    client_capabilities=ClientCapabilities(terminal=True),
                )
                assert initialized.agent_capabilities.load_session is True
                assert (
                    initialized.agent_capabilities.session_capabilities.resume is None
                )
                session = await connection.new_session(cwd=str(tmp_path))
                assert (
                    await connection.prompt(
                        session.session_id, [TextContentBlock(text="pwd")]
                    )
                ).stop_reason == "end_turn"
                while not any(
                    u.session_update == "agent_message_chunk" for _, u in client.updates
                ):
                    await asyncio.sleep(0)
                assert client.commands[0]["session_id"] == session.session_id
                assert client.commands[0]["args"] == ["-lc", "pwd"]
                assert any(u.session_update == "tool_call" for _, u in client.updates)
        finally:
            await connection.close()

        tape = Tape(tmp_path, store, TapeContext()).session_tape(
            f"acp-server:{session.session_id}", tmp_path
        )
        await store.append(
            tape.name,
            TapeEntry.message({"role": "assistant", "content": "saved history"}),
        )
        # Reconnect and load on a fresh WebSocket, then continue the same session.
        client = HTTPClient("reconnected output")
        client.release_command.set()
        connection = connect_to_agent(client, await create_websocket_stream(ws_url))
        try:
            async with asyncio.timeout(10):
                await connection.initialize(
                    protocol_version=1,
                    client_capabilities=ClientCapabilities(terminal=True),
                )
                await connection.load_session(
                    cwd=str(tmp_path), session_id=session.session_id
                )
                while not client.updates:
                    await asyncio.sleep(0)
                assert client.updates[0][1].content.text == "saved history"
                assert (
                    await connection.prompt(
                        session.session_id, [TextContentBlock(text="ls")]
                    )
                ).stop_reason == "end_turn"
                while not any(
                    u.session_update == "agent_message_chunk"
                    and u.content.text == client.label
                    for _, u in client.updates
                ):
                    await asyncio.sleep(0)
                assert {sid for sid, _ in client.updates} == {session.session_id}
                assert client.commands[0]["args"] == ["-lc", "ls"]
        finally:
            await connection.close()


@pytest.fixture
def http_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Capture the SDK factory to test Bub's state without network scheduling."""
    from bub.builtin import tools as builtin_tools  # noqa: F401

    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    factories = []

    def create_sdk_app(factory):
        factories.append(factory)

        async def sdk_app(scope, receive, send):
            pass

        return sdk_app

    monkeypatch.setattr(http_module, "create_asgi_app", create_sdk_app)
    app = create_http_app(HTTPFramework(tmp_path))
    return app, factories[0]


@pytest.mark.asyncio
async def test_http_connections_preserve_interleaved_session_writes(
    tmp_path: Path, http_app
) -> None:
    _, factory = http_app
    one, two = factory(HTTPClient("first")), factory(HTTPClient("second"))
    assert type(one) is type(two) is BubACPAgent
    first = await one.new_session(cwd=str(tmp_path))
    second = await two.new_session(cwd=str(tmp_path))
    expected = {first.session_id, second.session_id}
    assert {
        session.session_id for session in (await one.list_sessions()).sessions
    } == expected
    # Listing must not detach an agent from the shared store.
    third = await two.new_session(cwd=str(tmp_path))
    fourth = await one.new_session(cwd=str(tmp_path))
    expected.update((third.session_id, fourth.session_id))
    assert {
        session.session_id for session in (await two.list_sessions()).sessions
    } == expected
    stored = json.loads((tmp_path / "home" / "acp-sessions.json").read_text())
    assert {session["session_id"] for session in stored} == expected


@pytest.mark.asyncio
async def test_http_connections_serialize_prompts(tmp_path: Path, http_app) -> None:
    _, factory = http_app
    first, second = HTTPClient("first"), HTTPClient("second")
    one, two = factory(first), factory(second)
    for agent, client in [(one, first), (two, second)]:
        agent.on_connect(client)
        await agent.initialize(1, ClientCapabilities(terminal=True))
    session1 = await one.new_session(cwd=str(tmp_path))
    session2 = await two.new_session(cwd=str(tmp_path))
    prompt = [TextContentBlock(type="text", text="pwd")]
    task1 = asyncio.create_task(
        one.prompt(prompt=prompt, session_id=session1.session_id)
    )
    task2 = None
    try:
        async with asyncio.timeout(2):
            await first.command_started.wait()
            task2 = asyncio.create_task(
                two.prompt(prompt=prompt, session_id=session2.session_id)
            )
            await asyncio.sleep(0)  # Let the second prompt reach the shared lock.
            assert not second.command_started.is_set()
            first.release_command.set()
            second.release_command.set()
            await asyncio.gather(task1, task2)
            assert second.command_started.is_set()
    finally:
        first.release_command.set()
        second.release_command.set()
        for task in (task1, task2):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (task1, task2) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
async def test_http_shutdown_cancels_background_steering(
    tmp_path: Path, http_app
) -> None:
    app, factory = http_app
    client = HTTPClient("first")
    agent = factory(client)
    agent.on_connect(client)
    await agent.initialize(1, ClientCapabilities(terminal=True))
    session = await agent.new_session(cwd=str(tmp_path))
    original_bash = REGISTRY["bash"]
    tasks = []
    try:
        async with asyncio.timeout(2):
            await agent._execute_or_queue_steering(
                session.session_id, [TextContentBlock(type="text", text="pwd")]
            )
            await client.command_started.wait()
            tasks = list(agent._background_tasks)
            assert tasks
            await app({"type": "lifespan"}, None, None)
            assert all(task.cancelled() for task in tasks)
            assert not agent._background_tasks
            assert REGISTRY["bash"] is original_bash
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
async def test_http_runner_owns_framework_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    framework = HTTPFramework(tmp_path)
    running = False

    @asynccontextmanager
    async def lifetime():
        nonlocal running
        running = True
        try:
            yield
        finally:
            running = False

    async def run(app, config, *, shutdown_trigger):
        assert shutdown_trigger is None
        assert running
        assert callable(app)
        assert config.bind == [f"{host}:0"]
        assert config.certfile == str(tmp_path / "cert.pem")
        assert config.keyfile == str(tmp_path / "key.pem")
        assert config.alpn_protocols == ["h2", "http/1.1"]
        # Exercise Hypercorn's native IPv4/IPv6 bind parsing.
        sockets = config.create_sockets()
        for sock in sockets.secure_sockets:
            try:
                assert sock.getsockname()[0] == host
            finally:
                sock.close()

    monkeypatch.setattr(framework, "running", lifetime, raising=False)
    monkeypatch.setattr(http_module, "serve", run)
    await http_module.run_acp_http(
        framework,
        host=host,
        port=0,
        certfile=tmp_path / "cert.pem",
        keyfile=tmp_path / "key.pem",
    )
    assert not running


@pytest.mark.parametrize("port", [None, 9000])
@pytest.mark.parametrize("transport", ["http", "websocket"])
def test_cli_selects_web_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int | None, transport: str
) -> None:
    calls = []

    async def run(framework: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(http_module, "run_acp_http", run)
    app = typer.Typer()

    @app.callback()
    def root() -> None:
        pass

    ACPServerPlugin(HTTPFramework(tmp_path)).register_cli_commands(app)
    result = CliRunner().invoke(
        app,
        ["acp", "--transport", transport]
        + (["--port", str(port)] if port is not None else []),
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        {"host": "127.0.0.1", "port": port or 28200, "certfile": None, "keyfile": None}
    ]


@pytest.mark.parametrize(
    "args", [["--transport", "invalid"], ["--port", "0"], ["--port", "9000"]]
)
def test_cli_rejects_invalid_transport_options(tmp_path: Path, args: list[str]) -> None:
    app = typer.Typer()
    ACPServerPlugin(HTTPFramework(tmp_path)).register_cli_commands(app)
    assert CliRunner().invoke(app, args).exit_code == 2


@pytest.mark.parametrize("transport", ["http", "websocket"])
@pytest.mark.parametrize("tls_files", ["both", "cert", "key"])
def test_cli_web_tls_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str, tls_files: str
) -> None:
    calls = []

    async def run(framework, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(http_module, "run_acp_http", run)
    app = typer.Typer()
    ACPServerPlugin(HTTPFramework(tmp_path)).register_cli_commands(app)
    # Existing files satisfy CLI path validation; the mocked runner never reads them.
    cert = Path(__file__)
    key = Path(http_module.__file__)
    args = ["--transport", transport, "--host", "localhost", "--port", "29200"]
    if tls_files in ("both", "cert"):
        args.extend(["--certfile", str(cert)])
    if tls_files in ("both", "key"):
        args.extend(["--keyfile", str(key)])
    result = CliRunner().invoke(app, args)
    if tls_files == "both":
        assert result.exit_code == 0, result.output
        assert calls == [
            {"host": "localhost", "port": 29200, "certfile": cert, "keyfile": key}
        ]
    else:
        assert result.exit_code == 2
        assert "must be provided together" in result.output
        assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_source", ["channel", "gateway", "already_stopped"])
async def test_channel_lifecycle_reuses_gateway_framework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_source: str
) -> None:
    monkeypatch.setenv("BUB_ACP_SERVER_HOST", "localhost")
    monkeypatch.setenv("BUB_ACP_SERVER_PORT", "29200")
    framework = HTTPFramework(tmp_path)  # No running(): gateway owns that lifetime.
    channel = ACPHTTPChannel(framework)
    entered = asyncio.Event()
    calls = []

    async def run(app, config, *, shutdown_trigger):
        assert shutdown_trigger == stop.wait
        calls.append(config.bind)
        entered.set()
        await shutdown_trigger()

    monkeypatch.setattr(http_module, "serve", run)
    stop = asyncio.Event()
    await channel.stop()  # Stopping before startup is harmless.
    if stop_source == "already_stopped":
        stop.set()
    await channel.start(stop)
    task = channel._task
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if stop_source == "already_stopped":
            assert stop.is_set()  # start() must not clear the gateway's event.
        else:
            await channel.start(stop)
            assert channel._task is task
        if stop_source != "channel":
            stop.set()
            await asyncio.wait_for(asyncio.shield(task), 2)
    finally:
        await channel.stop()
    await channel.stop()
    assert task.done()
    assert channel._task is None
    assert stop.is_set()
    assert calls == [["localhost:29200"]]


@pytest.mark.asyncio
async def test_channel_reports_server_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*args, **kwargs):
        raise OSError("address in use")

    monkeypatch.setattr(http_module, "serve", fail)
    channel = ACPHTTPChannel(HTTPFramework(tmp_path))
    stop = asyncio.Event()
    await channel.start(stop)
    await asyncio.wait_for(stop.wait(), 2)
    await channel.stop()
    assert channel._task is None


def test_gateway_discovers_acp_only_when_explicitly_enabled(tmp_path: Path) -> None:
    framework = HTTPFramework(tmp_path)
    implementation = ACPServerPlugin(framework)
    framework.get_channels = lambda handler: {
        channel.name: channel for channel in implementation.provide_channels(handler)
    }
    manager = ChannelManager(framework, enabled_channels=["acp-server"])
    channel = manager.get_channel("acp-server")
    assert isinstance(channel, Interface)
    assert manager.enabled_channels() == [channel]
    assert not ChannelManager(framework, enabled_channels=["all"]).enabled_channels()


def test_gateway_cli_starts_and_stops_acp_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework._load_builtin_hooks()
    framework._plugin_manager.register(ACPServerPlugin(framework), name="acp-server")
    calls = []
    shutdown = []
    original_start = ACPHTTPChannel.start

    async def start(channel, stop_event):
        shutdown.append(stop_event)
        await original_start(channel, stop_event)

    async def run(app, config, *, shutdown_trigger):
        assert framework.get_steering_inbox() is not None
        assert framework.get_tape_store() is not None
        calls.append(config.bind)
        shutdown[0].set()
        await shutdown_trigger()
        calls.append("stopped")

    monkeypatch.setattr(ACPHTTPChannel, "start", start)
    monkeypatch.setattr(http_module, "serve", run)
    result = CliRunner().invoke(
        framework.create_cli_app(), ["gateway", "--enable-channel", "acp-server"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [["127.0.0.1:28200"], "stopped"]
    assert framework.get_tape_store() is None
    assert framework.get_steering_inbox() is None


@pytest.mark.parametrize(
    "values",
    [{"port": 0}, {"port": 65536}, {"certfile": "cert.pem"}, {"keyfile": "key.pem"}],
)
def test_http_settings_reject_invalid_values(values) -> None:
    with pytest.raises(ValueError):
        ACPServerSettings(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("tls", [False, True])
async def test_gateway_serves_http_without_cross_channel_tool_or_stream_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tls: bool
) -> None:
    from bub.builtin import tools as builtin_tools  # noqa: F401

    monkeypatch.setenv("BUB_HOME", str(tmp_path / "home"))
    local_calls = []

    async def local_bash(**kwargs):
        local_calls.append(kwargs)
        return "local output"

    original_bash = replace(REGISTRY["bash"], handler=local_bash)
    monkeypatch.setitem(REGISTRY, "bash", original_bash)

    class OtherChannel(Channel):
        name = "other"

        async def start(self, stop_event):
            pass

        async def stop(self):
            pass

        async def stream_events(self, message, stream):
            async for event in stream:
                if event.kind == "text":
                    other_text.append(event.data["delta"])
                yield event

    other_text = []
    framework = HTTPFramework(tmp_path)
    channel = ACPHTTPChannel(framework)
    framework.get_channels = lambda handler: {
        channel.name: channel,
        "other": OtherChannel(),
    }
    entries = []

    @asynccontextmanager
    async def running():
        entries.append("start")
        try:
            yield
        finally:
            entries.append("stop")

    framework.running = running
    manager = ChannelManager(framework, enabled_channels=["acp-server", "other"])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    fd = listener.detach()

    if tls:
        cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
        )

    async def serve_gateway(app, config, **kwargs):
        config.bind = [f"fd://{fd}"]
        config.graceful_timeout = 1
        if tls:
            config.certfile, config.keyfile = str(cert), str(key)
        await serve(app, config, **kwargs)

    monkeypatch.setattr(http_module, "serve", serve_gateway)
    gateway = asyncio.create_task(manager.listen_and_run())
    client = HTTPClient("remote output")
    connection = None
    prompt_task = None
    url = f"{'https' if tls else 'http'}://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(
            verify=False, http2=tls, timeout=None, trust_env=False
        ) as transport:
            async with asyncio.timeout(10):
                while True:
                    try:
                        response = await transport.get(url + "/missing")
                        assert response.status_code == 404
                        assert response.http_version == (
                            "HTTP/2" if tls else "HTTP/1.1"
                        )
                        break
                    except httpx.ConnectError:
                        if gateway.done():
                            await gateway
                        await asyncio.sleep(0.01)
                connection = connect_to_agent(
                    client, create_http_stream(url + "/acp", client=transport)
                )
                await connection.initialize(
                    protocol_version=1,
                    client_capabilities=ClientCapabilities(terminal=True),
                )
                session = await connection.new_session(cwd=str(tmp_path))
                prompt_task = asyncio.create_task(
                    connection.prompt(
                        session.session_id,
                        [TextContentBlock(type="text", text="remote command")],
                    )
                )
                await client.command_started.wait()
                assert framework.router is manager
                result = await framework.process_inbound(
                    ChannelMessage(
                        session_id="other:local",
                        channel="other",
                        chat_id="local",
                        content="local command",
                        is_active=True,
                        kind="normal",
                    ),
                    stream_output=True,
                )
                assert result.model_output == "local output"
                assert other_text == ["local output"]
                assert len(local_calls) == 1
                assert local_calls[0]["cmd"] == "local command"
                assert len(client.commands) == 1
                client.release_command.set()
                assert (await prompt_task).stop_reason == "end_turn"
                assert [
                    update.content.text
                    for _, update in client.updates
                    if update.session_update == "agent_message_chunk"
                ] == ["remote output"]
                assert framework.router is manager
                assert REGISTRY["bash"] is original_bash
                await connection.close()
                connection = None
    finally:
        client.release_command.set()
        if connection is not None:
            await connection.close()
        if prompt_task is not None:
            prompt_task.cancel()
            await asyncio.gather(prompt_task, return_exceptions=True)
        gateway.cancel()
        await asyncio.wait_for(gateway, 5)
    assert entries == ["start", "stop"]
    assert channel._task is None
    assert framework.router is None
