from __future__ import annotations

import asyncio
import json
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from acp import connect_to_agent
from acp.exceptions import RequestError
from acp.http import create_http_stream
from acp.schema import (
    ClientCapabilities,
    CreateTerminalResponse,
    TerminalOutputResponse,
    TextContentBlock,
    WaitForTerminalExitResponse,
)
from bub.model_selection import ModelOptions
from bub.streaming import StreamEvent
from bub.tools import REGISTRY, ToolContext
from bub.turn import TurnResult
from hypercorn.asyncio import serve
from hypercorn.config import Config
from typer.testing import CliRunner

from bub_acp_server import http as http_module
from bub_acp_server.plugin import ACPServerPlugin
from bub_acp_server.http import create_http_app
from bub_acp_server.steering import ACPSteeringInbox


class HTTPFramework:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.router: Any = None
        self.inbox = ACPSteeringInbox()

    def bind_channel_router(self, router: Any) -> None:
        self.router = router

    def get_steering_inbox(self) -> ACPSteeringInbox:
        return self.inbox

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
async def http_server(tmp_path: Path, *, tls: bool):
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
            create_http_app(HTTPFramework(tmp_path)),
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
                    assert initialized.agent_capabilities.load_session is False
                    assert (
                        initialized.agent_capabilities.session_capabilities.resume
                        is None
                    )
                    session1 = await one.new_session(cwd=str(tmp_path))
                    assert (
                        initialized.agent_capabilities.session_capabilities.close
                        is None
                    )
                    with pytest.raises(RequestError):
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

    async def run(app, config):
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
def test_cli_selects_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int | None
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
        ["acp", "--transport", "http"]
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
