"""External tools share native names, but not native child-turn ownership."""

import pytest
from ag_ui.core import Tool as ClientTool
from bub.core.tools import Tool, ToolContext
from bub_ag_ui.runtime import ProtocolError
from bub_ag_ui.tools import ExternalCallsPending, ExternalTools, InterruptTool

from .conftest import request


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
    with pytest.raises(ProtocolError, match="shadows"):
        await adapter.submit(
            "alice",
            request(tools=[{"name": client_name, "description": "", "parameters": {}}]),
        )
    assert provider.requests == []
    assert (await adapter.history("alice", "t1"))["runs"] == {}


@pytest.mark.parametrize("interrupt", [False, True])
async def test_external_definitions_cannot_share_model_alias(setup, interrupt):
    _, make = setup
    definition = {"name": "web.search", "description": "", "parameters": {}}
    adapter = make(interrupts=(InterruptTool(**definition),) if interrupt else ())
    tools = [{"name": "web_search", "description": "", "parameters": {}}]
    if not interrupt:
        tools.append(definition)
    with pytest.raises(ProtocolError, match="shadows"):
        await adapter.submit("alice", request(tools=tools))


@pytest.mark.parametrize("interrupt", [False, True])
async def test_direct_foreign_tool_call_cannot_modify_parent_pending(setup, interrupt):
    _, make = setup
    agent = make()._storage_agent
    state = {"session_id": "parent"}
    definition = {"name": "browser", "description": "", "parameters": {}}
    external = ExternalTools(
        [(InterruptTool if interrupt else ClientTool)(**definition)]
    )
    external.bind(agent, state)
    parent = ToolContext(tape=agent.tape("parent"), state=state, call_id="parent-call")
    with pytest.raises(ExternalCallsPending):
        await agent.tools["browser"].run(context=parent)
    # A shallow state copy retains all inherited values, not turn ownership.
    child = ToolContext(
        tape=agent.tape("child"), state=state.copy(), call_id="child-call"
    )
    with pytest.raises(ValueError, match="owning AG-UI turn"):
        await agent.tools["browser"].run(context=child)
    assert external.context is parent
    assert [call["id"] for call in external.pending] == ["parent-call"]
