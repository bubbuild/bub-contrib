"""External tools share native names, but not native child-turn ownership."""

import pytest
from bub.core.tools import Tool
from bub_ag_ui.tools import InterruptTool

from .conftest import collect, request


@pytest.mark.parametrize(
    ("server_name", "client_name"),
    [
        ("web.search", "web.search"),
        ("web.search", "web_search"),
        ("web_search", "web.search"),
    ],
)
async def test_native_tool_name_collisions_are_rejected(
    setup, server_name, client_name
):
    provider, make = setup
    adapter = make([Tool.from_callable(lambda: "server", name=server_name)])
    _, events = await collect(
        adapter,
        request(tools=[{"name": client_name, "description": "", "parameters": {}}]),
    )
    assert events[-1]["type"] == "RUN_ERROR"
    assert "shadows" in events[-1]["message"]
    assert provider.requests == []


@pytest.mark.parametrize("interrupt", [False, True])
async def test_external_definitions_cannot_share_model_alias(setup, interrupt):
    _, make = setup
    definition = {"name": "web.search", "description": "", "parameters": {}}
    adapter = make(interrupts=(InterruptTool(**definition),) if interrupt else ())
    tools = [{"name": "web_search", "description": "", "parameters": {}}]
    if not interrupt:
        tools.append(definition)
    _, events = await collect(adapter, request(tools=tools))
    assert events[-1]["type"] == "RUN_ERROR"
    assert "shadows" in events[-1]["message"]
