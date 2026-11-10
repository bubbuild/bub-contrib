import asyncio
from copy import deepcopy

import pytest
from ag_ui.core import ActivityDeltaEvent, ActivitySnapshotEvent
from bub_ag_ui.events import RunProjection
from bub_ag_ui.state import SharedState
from jsonpatch import JsonPatchConflict


async def test_invalid_activity_patch_does_not_change_response():
    projection = RunProjection()

    async def emit(event):
        projection.apply(event)

    state = SharedState({}, emit)
    await state.emit(
        ActivitySnapshotEvent(
            message_id="a1", activity_type="progress", content={"done": 1}
        )
    )
    await state.emit(
        ActivityDeltaEvent(
            message_id="a1",
            activity_type="progress",
            patch=[{"op": "replace", "path": "/done", "value": 2}],
        )
    )
    previous = deepcopy(projection.messages)
    with pytest.raises(JsonPatchConflict):
        await state.emit(
            ActivityDeltaEvent(
                message_id="a1",
                activity_type="progress",
                patch=[{"op": "remove", "path": "/does-not-exist"}],
            )
        )
    assert projection.messages == previous


async def test_concurrent_state_patches_are_serialized():
    events = []

    async def emit(event):
        await asyncio.sleep(0)
        events.append(event)

    state = SharedState({"items": []}, emit)
    await asyncio.gather(
        *(
            state.patch([{"op": "add", "path": "/items/-", "value": i}])
            for i in range(10)
        )
    )
    assert state.value == {"items": list(range(10))}
    assert len(events) == 10
