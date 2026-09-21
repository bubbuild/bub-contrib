from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from acp.schema import (
    AcceptElicitationResponse,
    CancelElicitationResponse,
    ClientCapabilities,
    DeclineElicitationResponse,
    ElicitationFormSessionMode,
)
from bub.tools import ToolContext

from bub_acp_server.client_tools import ACPClientToolRuntime, build_client_tools


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["minimal", "full"]}},
    "required": ["answer"],
}


def runtime_with(client: Any, elicitation: Any = None) -> ACPClientToolRuntime:
    runtime = ACPClientToolRuntime()
    runtime.connect(client)
    runtime.set_capabilities(ClientCapabilities(elicitation=elicitation))
    return runtime


def context() -> ToolContext:
    return ToolContext(
        tape=cast(Any, None), state={"session_id": "acp-server:session-1"}
    )


@pytest.mark.parametrize(
    ("capability", "supported"),
    [
        (None, False),
        ({}, False),
        ({"form": None}, False),
        ({"url": {}}, False),
        ({"form": {}}, True),
        ({"form": {}, "url": {}}, True),
    ],
)
def test_ask_user_requires_explicit_form_capability(capability, supported) -> None:
    tools = build_client_tools(runtime_with(SimpleNamespace(), capability))
    assert ("ask_user" in tools) == supported


def test_ask_user_exposes_form_schema_with_resolvable_references() -> None:
    tool = build_client_tools(runtime_with(SimpleNamespace(), {"form": {}}))["ask_user"]
    parameters = tool.parameters
    form_ref = parameters["properties"]["requested_schema"]["$ref"]
    form_schema = parameters["$defs"][form_ref.removeprefix("#/$defs/")]
    assert {"type", "properties", "required"} <= form_schema["properties"].keys()
    assert (
        "enum" in parameters["$defs"]["ElicitationStringPropertySchema"]["properties"]
    )
    assert (
        "minimum"
        in parameters["$defs"]["ElicitationNumberPropertySchema"]["properties"]
    )

    def check_references(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = parameters
                assert value["$ref"].startswith("#/")
                for part in value["$ref"][2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                check_references(child)
        elif isinstance(value, list):
            for child in value:
                check_references(child)

    check_references(parameters)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        AcceptElicitationResponse(content={"answer": "minimal"}),
        AcceptElicitationResponse(),
        DeclineElicitationResponse(),
        CancelElicitationResponse(),
    ],
)
async def test_ask_user_routes_form_and_returns_user_action(response) -> None:
    client = SimpleNamespace(create_elicitation=AsyncMock(return_value=response))
    tool = build_client_tools(runtime_with(client, {"form": {}}))["ask_user"]
    assert tool.parameters["required"] == ["message", "requested_schema"]
    result = await tool.run(
        message="Choose an approach", requested_schema=SCHEMA, context=context()
    )
    assert json.loads(result) == response.model_dump(exclude_none=True)
    client.create_elicitation.assert_awaited_once()
    request = client.create_elicitation.call_args.kwargs
    assert request["message"] == "Choose an approach"
    mode = request["mode"]
    assert isinstance(mode, ElicitationFormSessionMode)
    assert mode.session_id == "session-1"
    assert mode.requested_schema.required == ["answer"]
    assert mode.requested_schema.properties["answer"].enum == ["minimal", "full"]


@pytest.mark.asyncio
async def test_ask_user_rechecks_capability_before_request() -> None:
    client = SimpleNamespace(create_elicitation=AsyncMock())
    runtime = runtime_with(client, {"form": {}})
    tool = build_client_tools(runtime)["ask_user"]
    runtime.set_capabilities(None)
    with pytest.raises(RuntimeError, match="does not support form elicitation"):
        await tool.run(message="Choose", requested_schema=SCHEMA, context=context())
    client.create_elicitation.assert_not_called()


@pytest.mark.asyncio
async def test_ask_user_propagates_turn_cancellation() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait_for_user(**kwargs):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client = SimpleNamespace(create_elicitation=wait_for_user)
    tool = build_client_tools(runtime_with(client, {"form": {}}))["ask_user"]
    task = asyncio.create_task(
        tool.run(message="Choose", requested_schema=SCHEMA, context=context())
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set()
