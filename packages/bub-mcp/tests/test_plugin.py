from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from any_llm.types.completion import ChatCompletion
from bub.builtin.agent import Agent
from bub.builtin.codemode import run_code
from bub.framework import BubFramework
from bub.tape import Tape
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
        # Repeated discovery keeps the same source precedence.
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
    assert embedded.known_tools[remote.name].run() == "ok"
    assert remote.name not in default.tools
    assert remote.name not in REGISTRY
    # Shutdown must not remove a replacement installed by someone else.
    replacement = plugin.Tool(name=remote.name, handler=lambda: "new")
    embedded.tools[remote.name] = replacement
    await channel.stop()
    assert embedded.tools[remote.name] is replacement


@pytest.mark.parametrize("code_mode", [False, True])
@pytest.mark.parametrize(
    ("allowed", "excluded", "visible"),
    [
        (None, [], ["alpha", "beta"]),
        ([], [], []),
        (["mcp.*"], ["mcp_beta_weather_get_forecast"], ["alpha"]),
        (["mcp_alpha_weather_get_forecast"], ["mcp.alpha_weather_get_forecast"], []),
    ],
)
async def test_mcp_discovery_scope_and_cleanup_with_independent_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code_mode: bool,
    allowed: list[str] | None,
    excluded: list[str],
    visible: list[str],
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    monkeypatch.setattr(plugin, "_create_fastmcp_client", FakeClient)
    replies: list[str | tuple[str, dict[str, Any]]] = []
    requests: list[dict[str, Any]] = []

    class Provider:
        SUPPORTS_COMPLETION_STREAMING = False

        async def acompletion(self, **kwargs: Any) -> ChatCompletion:
            requests.append(kwargs)
            reply = replies.pop(0)
            message: dict[str, Any] = {"role": "assistant", "content": reply}
            if isinstance(reply, tuple):
                name, arguments = reply
                function = {"name": name, "arguments": json.dumps(arguments)}
                call = {
                    "id": str(len(requests)),
                    "type": "function",
                    "function": function,
                }
                message = {"role": "assistant", "tool_calls": [call]}
            choice = {
                "index": 0,
                "message": message,
                "finish_reason": "stop" if isinstance(reply, str) else "tool_calls",
            }
            return ChatCompletion(
                id="reply",
                model="test",
                created=0,
                object="chat.completion",
                choices=[choice],
            )

    monkeypatch.setattr(
        "bub.builtin.model_runner.AnyLLM.create", lambda *a, **k: Provider()
    )
    channels = [
        plugin.MCPChannel.from_server_configs({name: {"command": name}})
        for name in ("alpha", "beta")
    ]
    for channel in channels:
        channel.settings = plugin.MCPSettings(
            allowed_tools=allowed, excluded_tools=excluded
        )

    async def uppercase(
        tools: list[plugin.Tool], tape: Tape
    ) -> tuple[list[plugin.Tool], str]:
        return [
            replace(item, renderer=str.upper) if item.name.startswith("mcp.") else item
            for item in tools
        ], ""

    async def run(agent: Agent, session: str = "task") -> list[dict[str, Any]]:
        requests.clear()
        replies.append("done")
        stream = await agent.run_stream(
            session_id=session,
            prompt="Read forecasts.",
            model="openrouter:test",
            state={"code_mode": code_mode},
        )
        async for _ in stream:
            pass
        return list(requests)

    def names(request: dict[str, Any]) -> list[str]:
        return [item["function"]["name"] for item in request.get("tools") or []]

    def call(name: str) -> tuple[str, dict[str, Any]]:
        alias = f"mcp_{name}_weather_get_forecast"
        return (
            ("run_code", {"code": f"print(await tools.{alias}(city='{name}'))"})
            if code_mode
            else (alias, {"city": name})
        )

    try:
        for channel in channels:
            await channel.connect()
        agent = Agent(framework, tools=[REGISTRY["tape.info"], run_code], skill_dirs=[])
        agent.tool_providers.append(uppercase)
        for channel in channels:
            channel.bind_agent(agent)
        replies.append(
            ("run_code", {"code": "print(await tools.tape_info())"})
            if code_mode
            else ("tape_info", {})
        )
        for name in reversed(visible):
            if not code_mode:
                replies.append(
                    ("mcp_describe", {"names": [f"mcp_{name}_weather_get_forecast"]})
                )
            replies.append(call(name))
        first = await run(agent)
        assert not any(
            name.startswith("mcp_") and name != "mcp_describe"
            for name in names(first[0])
        )
        if not code_mode:
            assert names(first[-1]) == [
                *names(first[0]),
                *(f"mcp_{name}_weather_get_forecast" for name in reversed(visible)),
            ]
            prompts = [
                next(m["content"] for m in r["messages"] if m["role"] == "system")
                for r in first
            ]
            for name in ("alpha", "beta"):
                assert (f"mcp_{name}_weather_get_forecast" in prompts[0]) == (
                    name in visible
                )
            assert all(prompt == prompts[0] for prompt in prompts)
        results = "\n".join(
            m.get("content", "") for m in first[-1]["messages"] if m["role"] == "tool"
        )
        assert "entries" in results
        for name in visible:
            expected = f"forecast for {name}"
            assert (expected if code_mode else expected.upper()) in results
        if visible and not code_mode:
            replies.append(call(visible[0]))
        fresh = await run(agent, "fresh")
        if visible and not code_mode:
            assert any(
                "does not exist" in m.get("content", "")
                for m in fresh[-1]["messages"]
                if m["role"] == "tool"
            )
        for name, channel in zip(("alpha", "beta"), channels, strict=True):
            assert channel.list()[name].client.tool_calls == (
                [("weather_get_forecast", {"city": name})] if name in visible else []
            )
        assert not any(
            n.startswith("mcp_") and n != "mcp_describe" for n in names(fresh[0])
        )
        for channel, name in zip(channels, ("alpha", "beta"), strict=True):
            await channel.stop()
            remaining = await run(agent)
            assert f"mcp_{name}_weather_get_forecast" not in names(remaining[0])
        assert "mcp_describe" not in names(remaining[0])
    finally:
        for channel in channels:
            await channel.stop()
