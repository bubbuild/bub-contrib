from __future__ import annotations

import asyncio
from pathlib import Path

from bub.tools import REGISTRY
from bub_mcp import plugin


class FakeTextContent:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeCallToolResult:
    def __init__(
        self,
        *,
        content: list[object] | None = None,
        structured_content: object | None = None,
        is_error: bool = False,
    ) -> None:
        self.content = content or []
        self.structuredContent = structured_content
        self.isError = is_error


class FakeRemoteTool:
    def __init__(
        self, name: str, description: str, input_schema: dict[str, object]
    ) -> None:
        self.name = name
        self.description = description
        self.inputSchema = input_schema


class FakeClient:
    def __init__(
        self, config: dict[str, object], *, init_timeout_seconds: float | None
    ) -> None:
        self.config = config
        self.init_timeout_seconds = init_timeout_seconds
        self.entered = False
        self.exited = False
        self.tool_calls: list[tuple[str, dict[str, object]]] = []

    async def __aenter__(self) -> FakeClient:
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.exited = True
        return False

    async def list_tools(self) -> list[FakeRemoteTool]:
        return [
            FakeRemoteTool(
                "weather_get_forecast",
                "Get forecast from remote MCP server.",
                {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            )
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, object]
    ) -> FakeCallToolResult:
        self.tool_calls.append((name, arguments))
        return FakeCallToolResult(
            content=[FakeTextContent(f"forecast for {arguments['city']}")]
        )


def _write_config(tmp_path: Path, body: str) -> None:
    (tmp_path / "mcp.json").write_text(body, encoding="utf-8")


def _make_channel(tmp_path: Path) -> plugin.MCPChannel:
    channel = plugin.MCPChannel()
    channel.settings.config_path = tmp_path / "mcp.json"
    return channel


def teardown_function() -> None:
    for name in list(REGISTRY):
        if name.startswith(plugin.TOOL_PREFIX):
            REGISTRY.pop(name, None)


def test_lifecycle_channel_uses_manager_start_and_stop(monkeypatch) -> None:
    channel = plugin.MCPChannel()
    calls: list[str] = []

    async def fake_bootstrap(stop_event: asyncio.Event) -> None:
        del stop_event
        calls.append("start")
        channel._servers["weather"] = plugin.MCPServerState(
            client=object(), connected=True
        )

    async def fake_close_client(client) -> None:
        del client
        calls.append("stop")

    monkeypatch.setattr(channel, "_bootstrap", fake_bootstrap)
    monkeypatch.setattr(channel, "_close_client", fake_close_client)

    async def run_test() -> None:
        await channel.start(asyncio.Event())
        assert channel._bootstrap_task is not None
        await channel._bootstrap_task
        await channel.stop()

    asyncio.run(run_test())

    assert calls == ["start", "stop"]


def test_bootstrap_registers_remote_tools_and_forwards_calls(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        '{"mcpServers":{"weather":{"url":"https://weather.example.com/mcp","transport":"http"}}}',
    )

    created_clients: list[FakeClient] = []

    def fake_create_fastmcp_client(
        config: dict[str, object], *, init_timeout_seconds: float | None
    ) -> FakeClient:
        client = FakeClient(config, init_timeout_seconds=init_timeout_seconds)
        created_clients.append(client)
        return client

    monkeypatch.setattr(plugin, "_create_fastmcp_client", fake_create_fastmcp_client)

    channel = _make_channel(tmp_path)

    async def start_channel() -> None:
        await channel.start(asyncio.Event())
        assert channel._bootstrap_task is not None
        await channel._bootstrap_task

    asyncio.run(start_channel())

    tool_name = "mcp.weather_get_forecast"
    assert tool_name in REGISTRY
    assert created_clients[0].entered is True
    assert created_clients[0].config == {
        "weather": {
            "url": "https://weather.example.com/mcp",
            "transport": "http",
        }
    }
    assert set(channel.list()) == {"weather"}
    assert channel._servers["weather"].connected is True
    assert channel._servers["weather"].error is None
    assert channel._servers["weather"].client is created_clients[0]
    assert [tool.name for tool in channel._servers["weather"].tools] == [tool_name]

    result = asyncio.run(REGISTRY[tool_name].run(city="Paris"))

    assert result == "forecast for Paris"
    assert created_clients[0].tool_calls == [
        ("weather_get_forecast", {"city": "Paris"})
    ]

    asyncio.run(channel.stop())

    assert created_clients[0].exited is True
    assert tool_name in REGISTRY


def test_channel_list_reads_current_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    channel = _make_channel(tmp_path)

    assert channel.list() == {}


def test_bootstrap_records_failed_server_and_keeps_successful_servers(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        (
            '{"mcpServers":{'
            '"weather":{"url":"https://weather.example.com/mcp","transport":"http"},'
            '"broken":{"url":"https://broken.example.com/mcp","transport":"http"}'
            "}}"
        ),
    )

    created_clients: list[FakeClient] = []
    warnings: list[str] = []

    class FailingClient(FakeClient):
        async def __aenter__(self) -> FakeClient:
            self.entered = True
            raise RuntimeError("connection refused")

    def fake_create_fastmcp_client(
        config: dict[str, object], *, init_timeout_seconds: float | None
    ) -> FakeClient:
        server_name = next(iter(config))
        if server_name == "broken":
            return FailingClient(config, init_timeout_seconds=init_timeout_seconds)
        client = FakeClient(config, init_timeout_seconds=init_timeout_seconds)
        created_clients.append(client)
        return client

    def fake_warning(message: str, *args: object) -> None:
        warnings.append(message.format(*args))

    monkeypatch.setattr(plugin, "_create_fastmcp_client", fake_create_fastmcp_client)
    monkeypatch.setattr(plugin.logger, "warning", fake_warning)

    channel = _make_channel(tmp_path)

    async def start_channel() -> None:
        await channel.start(asyncio.Event())
        assert channel._bootstrap_task is not None
        await channel._bootstrap_task

    asyncio.run(start_channel())

    assert set(channel.list()) == {"weather", "broken"}
    assert channel._servers["weather"].connected is True
    assert channel._servers["weather"].error is None
    assert channel._servers["weather"].client is created_clients[0]
    assert channel._servers["broken"].connected is False
    assert channel._servers["broken"].error == "connection refused"
    assert channel._servers["broken"].client is None
    assert channel._servers["broken"].tools == []
    assert any("broken" in warning for warning in warnings)
    assert REGISTRY["mcp.weather_get_forecast"] is not None

    asyncio.run(channel.stop())

    assert created_clients[0].exited is True


def test_bootstrap_connects_servers_in_parallel(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        (
            '{"mcpServers":{'
            '"weather":{"url":"https://weather.example.com/mcp","transport":"http"},'
            '"calendar":{"url":"https://calendar.example.com/mcp","transport":"http"}'
            "}}"
        ),
    )

    channel = _make_channel(tmp_path)
    started: list[str] = []
    release = asyncio.Event()
    all_started = asyncio.Event()

    async def fake_connect_server(
        server_name: str, server_config: dict[str, object]
    ) -> plugin.MCPServerState:
        del server_config
        started.append(server_name)
        if len(started) == 2:
            all_started.set()
        await release.wait()

        async def fake_handler(**payload: object) -> str:
            del payload
            return "ok"

        return plugin.MCPServerState(
            client=FakeClient({server_name: {}}, init_timeout_seconds=None),
            tools=[
                plugin.Tool(
                    name=f"mcp.{server_name}_tool",
                    description=f"Tool for {server_name}",
                    parameters={"type": "object", "properties": {}},
                    handler=fake_handler,
                )
            ],
            connected=True,
        )

    monkeypatch.setattr(channel, "_connect_server", fake_connect_server)

    async def run_test() -> None:
        bootstrap_task = asyncio.create_task(channel._bootstrap(asyncio.Event()))
        await asyncio.wait_for(all_started.wait(), timeout=1)
        assert set(started) == {"weather", "calendar"}
        release.set()
        await bootstrap_task

    asyncio.run(run_test())

    assert set(channel.list()) == {"calendar", "weather"}
    assert [tool.name for tool in channel._servers["calendar"].tools] == [
        "mcp.calendar_tool"
    ]
    assert [tool.name for tool in channel._servers["weather"].tools] == [
        "mcp.weather_tool"
    ]


def test_channel_add_persists_changes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    _write_config(tmp_path, "{}")

    channel = _make_channel(tmp_path)

    result = asyncio.run(
        channel.add(
            "weather",
            {
                "url": "https://weather.example.com/mcp",
                "transport": "http",
            },
        )
    )

    assert result == {
        "weather": {
            "url": "https://weather.example.com/mcp",
            "transport": "http",
        }
    }
    assert channel.settings.read_mcp_servers() == result


def test_channel_remove_persists_changes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        '{"mcpServers":{"weather":{"url":"https://weather.example.com/mcp","transport":"http"}}}',
    )

    channel = _make_channel(tmp_path)

    result = asyncio.run(channel.remove("weather"))

    assert result == {}
    assert channel.settings.read_mcp_servers() == {}


def test_format_tool_result_uses_structured_content_when_text_is_missing() -> None:
    result = plugin._format_tool_result(
        FakeCallToolResult(structured_content={"status": "ok", "count": 2})
    )

    assert '"status": "ok"' in result
    assert '"count": 2' in result
