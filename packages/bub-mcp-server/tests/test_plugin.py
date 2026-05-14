from __future__ import annotations

import asyncio
from dataclasses import dataclass

from bub_mcp_server import plugin


@dataclass
class FakeTurnResult:
    model_output: str


class FakeFramework:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def process_inbound(self, inbound: object) -> FakeTurnResult:
        self.messages.append(inbound)
        return FakeTurnResult(model_output=f"reply: {inbound.content}")


def test_mcp_server_exposes_only_run_model_tool() -> None:
    channel = plugin.MCPServerChannel(FakeFramework())
    server = channel._build_server()

    async def run_test() -> None:
        tools = await server.list_tools()
        assert [tool.name for tool in tools] == ["run_model"]

    asyncio.run(run_test())


def test_run_model_tool_forwards_prompt_to_bub_framework() -> None:
    framework = FakeFramework()
    channel = plugin.MCPServerChannel(framework)
    server = channel._build_server()

    async def run_test() -> None:
        result = await server.call_tool(
            "run_model",
            {"prompt": "hello", "session_id": "mcp:session-1"},
        )
        assert result.content[0].text == "reply: hello"

    asyncio.run(run_test())

    assert len(framework.messages) == 1
    inbound = framework.messages[0]
    assert inbound.session_id == "mcp:session-1"
    assert inbound.channel == "mcp-server"
    assert inbound.chat_id == "mcp:session-1"
    assert inbound.content == "hello"
    assert inbound.is_active is True


def test_channel_start_runs_sse_server_and_stop_cancels_task(monkeypatch) -> None:
    channel = plugin.MCPServerChannel(FakeFramework())
    calls: list[tuple[str, object]] = []
    entered = asyncio.Event()

    async def fake_run_server(server) -> None:
        calls.append(("run", server))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(channel, "_run_server", fake_run_server)

    async def run_test() -> None:
        await channel.start(asyncio.Event())
        assert channel.server is not None
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert channel._task is not None
        await channel.stop()
        assert channel._task is None

    asyncio.run(run_test())

    assert calls == [("run", channel.server)]
