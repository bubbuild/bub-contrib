from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bub.builtin.agent import Agent
from bub.framework import BubFramework
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
        self.structured_content = structured_content
        self.is_error = is_error


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

    async def close(self) -> None:
        self.exited = True

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


def test_bootstrap_discovers_remote_tools_without_global_registration(
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
    assert tool_name not in REGISTRY
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

    result = asyncio.run(channel.tools[tool_name].run(city="Paris"))

    assert result == "forecast for Paris"
    assert created_clients[0].tool_calls == [
        ("weather_get_forecast", {"city": "Paris"})
    ]

    asyncio.run(channel.stop())

    assert created_clients[0].exited is True
    assert tool_name not in REGISTRY


def test_channel_list_reads_current_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    channel = _make_channel(tmp_path)

    assert channel.list() == {}


def test_embedded_channel_can_keep_runtime_alive_when_all_servers_fail(
    monkeypatch,
) -> None:
    class EmbeddedChannel(plugin.MCPChannel):
        stop_when_all_failed = False

    channel = EmbeddedChannel.from_server_configs({"broken": {"command": "broken"}})

    async def fake_connect_server(
        server_name: str, server_config: dict[str, object]
    ) -> plugin.MCPServerState:
        del server_name, server_config
        return plugin.MCPServerState(error="connection refused")

    monkeypatch.setattr(channel, "_connect_server", fake_connect_server)

    async def run_test() -> None:
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        async with asyncio.timeout(1):
            while "broken" not in channel.list():
                await asyncio.sleep(0)
        assert not stop_event.is_set()
        await channel.stop()

    asyncio.run(run_test())


def test_default_channel_still_stops_runtime_when_all_servers_fail(monkeypatch) -> None:
    channel = plugin.MCPChannel.from_server_configs({"broken": {"command": "broken"}})

    async def fake_connect_server(
        server_name: str, server_config: dict[str, object]
    ) -> plugin.MCPServerState:
        del server_name, server_config
        return plugin.MCPServerState(error="connection refused")

    monkeypatch.setattr(channel, "_connect_server", fake_connect_server)

    async def run_test() -> None:
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        await asyncio.wait_for(stop_event.wait(), timeout=1)
        await channel.stop()

    asyncio.run(run_test())


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
    assert channel.tools["mcp.weather_get_forecast"] is not None

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


def test_tool_result_value_prefers_structured_content_for_typed_tools() -> None:
    value = plugin._tool_result_value(
        FakeCallToolResult(
            content=[FakeTextContent('{"status": "ok", "count": 2}')],
            structured_content={"status": "ok", "count": 2},
        ),
        mode="structured",
    )

    assert value == {"status": "ok", "count": 2}
    rendered = plugin._render_tool_value(value)
    assert '"status": "ok"' in rendered
    assert '"count": 2' in rendered


def test_untyped_tools_return_text_even_with_structured_content() -> None:
    remote_tool = FakeRemoteTool(
        "get_forecast", "Get forecast.", {"type": "object", "properties": {}}
    )
    output_schema, mode = plugin._output_schema(remote_tool)
    value = plugin._tool_result_value(
        FakeCallToolResult(
            content=[FakeTextContent("forecast for Paris")],
            structured_content={"city": "Paris"},
        ),
        mode=mode,
    )

    assert output_schema == {"type": "string"}
    assert value == "forecast for Paris"
    assert plugin._render_tool_value(value) == "forecast for Paris"


def test_build_tool_forwards_remote_output_schema() -> None:
    output_schema = {
        "type": "object",
        "properties": {"temperature": {"type": "number"}},
        "required": ["temperature"],
    }
    remote_tool = FakeRemoteTool(
        "get_forecast", "Get forecast.", {"type": "object", "properties": {}}
    )
    remote_tool.outputSchema = output_schema

    tool = plugin.MCPChannel()._build_tool("weather", remote_tool)

    assert tool is not None
    assert tool.output_schema == output_schema
    assert tool.render({"temperature": 21.5}) == '{\n  "temperature": 21.5\n}'


def test_fastmcp_wrapped_results_are_unwrapped() -> None:
    remote_tool = FakeRemoteTool(
        "describe", "Describe.", {"type": "object", "properties": {}}
    )
    remote_tool.outputSchema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
        "x-fastmcp-wrap-result": True,
    }

    output_schema, mode = plugin._output_schema(remote_tool)
    value = plugin._tool_result_value(
        FakeCallToolResult(
            content=[FakeTextContent("hello")], structured_content={"result": "hello"}
        ),
        mode=mode,
    )

    assert output_schema == {"type": "string"}
    assert value == "hello"


@pytest.mark.asyncio
async def test_late_discovery_reaches_existing_agent_and_stop_restores_tools(
    monkeypatch, tmp_path: Path
) -> None:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    mcp_plugin = plugin.MCPPlugin(framework)
    framework.plugin_manager.register(mcp_plugin, name="mcp")
    builtin = framework.plugin_manager.get_plugin("builtin")
    agent = builtin._get_agent()
    unrelated = Agent(framework)
    original_registry = REGISTRY.copy()
    original = plugin.Tool(name="mcp.weather_get_forecast", handler=lambda: "original")
    agent.tools[original.name] = original
    channel = mcp_plugin._manager
    channel._server_configs = {"weather": {"command": "weather"}}
    monkeypatch.setattr(plugin, "_create_fastmcp_client", FakeClient)

    await channel.start(asyncio.Event())
    try:
        # build_state waits for discovery, even though the Agent predates it.
        state = await framework.build_state({}, "test:weather")
        assert state["_runtime_agent"] is agent
        events = await agent.run_stream(
            session_id="test:weather",
            prompt=',mcp.weather_get_forecast city="Paris"',
            state=state,
        )
        output = [
            event.data.get("text") async for event in events if event.kind == "final"
        ]
        assert output == ["forecast for Paris"]
        assert REGISTRY == original_registry
        assert original.name not in unrelated.tools
        # Repeated binding must not save our own tool as the collision fallback.
        await framework.build_state({}, "test:weather")
    finally:
        await channel.stop()
    assert agent.tools[original.name] is original
    assert channel.tools == {}


@pytest.mark.asyncio
async def test_mcp_binds_explicit_runtime_agent_without_changing_default(
    tmp_path: Path,
) -> None:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    default = framework.plugin_manager.get_plugin("builtin")._get_agent()
    embedded = Agent(framework, tools=[])
    channel = plugin.MCPChannel()
    remote = plugin.Tool(name="mcp.embedded", handler=lambda: "ok")
    channel._servers["embedded"] = plugin.MCPServerState(tools=[remote], connected=True)

    await channel.bind_runtime_tools(framework, {"_runtime_agent": embedded})
    assert embedded.tools[remote.name] is remote
    assert remote.name not in default.tools
    assert remote.name not in REGISTRY
    # Shutdown must not remove a replacement installed by someone else.
    replacement = plugin.Tool(name=remote.name, handler=lambda: "new")
    embedded.tools[remote.name] = replacement
    await channel.stop()
    assert embedded.tools[remote.name] is replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("code_mode", [False, True])
@pytest.mark.parametrize(
    ("allowed", "excluded", "expected"),
    [
        (None, [], {"mcp_weather_get_forecast", "mcp_weather_archive"}),
        ([], [], set()),
        (["mcp_weather_get_forecast"], [], {"mcp_weather_get_forecast"}),
        (["mcp.weather_*"], ["*_archive"], {"mcp_weather_get_forecast"}),
        (["mcp.weather_get_forecast"], ["mcp_weather_get_forecast"], set()),
    ],
)
async def test_configured_selection_controls_model_requests_and_remote_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    code_mode: bool,
    allowed: list[str] | None,
    excluded: list[str],
    expected: set[str],
) -> None:
    import json
    from typing import Any

    from any_llm.types.completion import ChatCompletion
    from bub.builtin.codemode import run_code
    from fastmcp import Client, FastMCP

    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    monkeypatch.setenv("BUB_MCP_EXCLUDED_TOOLS", json.dumps(excluded))
    if allowed is None:
        monkeypatch.delenv("BUB_MCP_ALLOWED_TOOLS", raising=False)
    else:
        monkeypatch.setenv("BUB_MCP_ALLOWED_TOOLS", json.dumps(allowed))
    calls: list[tuple[str, dict[str, object]]] = []
    requests: list[dict[str, Any]] = []

    server = FastMCP("weather")

    @server.tool(name="weather_get_forecast")
    def forecast(city: str) -> str:
        """Get forecast from remote MCP server."""
        calls.append(("weather_get_forecast", {"city": city}))
        return f"forecast for {city}"

    @server.tool
    def archive(city: str) -> str:
        """Archive weather."""
        calls.append(("archive", {"city": city}))
        return f"archived {city}"

    class Provider:
        SUPPORTS_COMPLETION_STREAMING = False

        async def acompletion(self, **kwargs: Any) -> ChatCompletion:
            requests.append(kwargs)
            message: dict[str, Any] = {"role": "assistant", "content": "done"}
            if (code_mode and len(requests) == 1) or (
                not code_mode and expected and len(requests) <= 2
            ):
                if code_mode:
                    name = "run_code"
                    arguments = {
                        "code": "try:\n    print(await tools.mcp_weather_get_forecast(city='Paris'))\nexcept Exception as exc:\n    print(exc)"
                    }
                elif len(requests) == 1:
                    name = "mcp_describe"
                    arguments = {"names": ["mcp_weather_get_forecast"]}
                else:
                    name = "mcp_weather_get_forecast"
                    arguments = {"city": "Paris"}
                message = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-weather",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            return ChatCompletion.model_validate(
                {
                    "id": "reply",
                    "model": "test-model",
                    "created": 0,
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls"
                            if "tool_calls" in message
                            else "stop",
                            "message": message,
                        }
                    ],
                }
            )

    monkeypatch.setattr(
        plugin, "_create_fastmcp_client", lambda *args, **kwargs: Client(server)
    )
    monkeypatch.setattr(
        "bub.builtin.model_runner.AnyLLM.create", lambda *args, **kwargs: Provider()
    )
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    agent = Agent(framework, tools=[run_code] if code_mode else [], skill_dirs=[])
    channel = plugin.MCPChannel.from_server_configs({"weather": {"command": "weather"}})
    await channel.connect()
    try:
        channel.bind_agent(agent)
        stream = await agent.run_stream(
            session_id="test:selection",
            prompt="Get the forecast for Paris.",
            model="openrouter:test-model",
            state={"code_mode": code_mode, "_runtime_workspace": str(tmp_path)},
        )
        events = [event async for event in stream]
    finally:
        await channel.stop()

    assert any(
        event.kind == "final" and event.data.get("text") == "done" for event in events
    )
    definitions = requests[0].get("tools") or []
    assert {item["function"]["name"] for item in definitions} == (
        {"run_code"} if code_mode else {"mcp_describe"} if expected else set()
    )
    if not code_mode:
        catalog = "\n".join(
            message["content"]
            for message in requests[0]["messages"]
            if message["role"] == "system"
        )
        for name in {"mcp_weather_get_forecast", "mcp_weather_archive"}:
            assert (name in catalog) == (name in expected)
        if expected:
            assert {item["function"]["name"] for item in requests[1]["tools"]} == {
                "mcp_describe",
                "mcp_weather_get_forecast",
            }
    if expected:
        assert calls == [("weather_get_forecast", {"city": "Paris"})]
        assert any(
            message.get("content") == "forecast for Paris"
            or message.get("content") == "forecast for Paris\n"
            for message in requests[-1]["messages"]
            if message["role"] == "tool"
        )
    else:
        assert calls == []
        if code_mode:
            assert not any(
                "forecast for Paris" in message.get("content", "")
                for message in requests[1]["messages"]
                if message["role"] == "tool"
            )
