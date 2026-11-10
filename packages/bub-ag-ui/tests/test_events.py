"""Application state and activity updates are visible in the protocol output."""

import asyncio
import json

import jsonpatch
from ag_ui.core import ActivityDeltaEvent, ActivitySnapshotEvent
from bub.core.tools import Tool
from bub_ag_ui.state import shared_state

from .conftest import collect, request


async def test_invalid_activity_patch_leaves_last_valid_client_view(setup):
    provider, make = setup

    async def update(context):
        state = shared_state(context)
        await state.emit(
            ActivitySnapshotEvent(
                message_id="a1",
                activity_type="progress",
                content={"done": 1},
            )
        )
        await state.emit(
            ActivityDeltaEvent(
                message_id="a1",
                activity_type="progress",
                patch=[{"op": "replace", "path": "/done", "value": 2}],
            )
        )
        await state.emit(
            ActivityDeltaEvent(
                message_id="a1",
                activity_type="progress",
                patch=[{"op": "remove", "path": "/does-not-exist"}],
            )
        )

    adapter = make([Tool.from_callable(update, context=True)])
    provider.reply(calls=[("update", "update", "{}")])
    provider.reply("Could not apply that update")
    view, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [
        m["content"]
        for m in (await view.history())["messages"]
        if m["role"] == "activity"
    ] == [{"done": 2}]
    assert len([e for e in events if e["type"] == "ACTIVITY_DELTA"]) == 1


async def test_concurrent_state_updates_reach_client_without_lost_items(setup):
    provider, make = setup

    async def update(context):
        state = shared_state(context)
        await state.set({"items": []})
        await asyncio.gather(
            *(
                state.patch([{"op": "add", "path": "/items/-", "value": i}])
                for i in range(10)
            )
        )
        return state.value

    provider.reply(calls=[("update", "update", "{}")])
    provider.reply("updated")
    _, events = await collect(
        make([Tool.from_callable(update, context=True)]), request()
    )
    client_state = None
    for event in events:
        if event["type"] == "STATE_SNAPSHOT":
            client_state = event["snapshot"]
        elif event["type"] == "STATE_DELTA":
            client_state = jsonpatch.apply_patch(client_state, event["delta"])
    assert events[-1]["type"] == "RUN_FINISHED"
    assert sorted(client_state["items"]) == list(range(10))
    assert (
        next(
            json.loads(e["content"]) for e in events if e["type"] == "TOOL_CALL_RESULT"
        )
        == client_state
    )
