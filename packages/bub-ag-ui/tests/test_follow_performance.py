"""Subscribers need durable run events, not a projection of the conversation."""

import time
from types import SimpleNamespace

import pytest
from bub_ag_ui.events import Projection, wire_event


@pytest.mark.parametrize("deltas", [400, 800, 1600])
async def test_follow_does_not_reproject_history(setup, monkeypatch, deltas):
    _, make = setup
    adapter = make()
    thread = await adapter.thread("alice", "t1")
    await thread.append("admit", runId="r1", fingerprint="test")
    adapter._runs[thread.tape.name] = SimpleNamespace(run_id="r1")
    projections = 0
    reads = 0
    apply = Projection.apply
    fetch = thread.tape.store.fetch_all

    def counted_apply(self, event):
        nonlocal projections
        projections += 1
        return apply(self, event)

    async def counted_fetch(query):
        nonlocal reads
        reads += 1
        return await fetch(query)

    monkeypatch.setattr(Projection, "apply", counted_apply)
    monkeypatch.setattr(thread.tape.store, "fetch_all", counted_fetch)
    follower = adapter.events(thread, "r1")
    started = time.perf_counter()
    events = [
        wire_event("RUN_STARTED", threadId="t1", runId="r1"),
        wire_event("TEXT_MESSAGE_START", messageId="m1", role="assistant"),
        *[
            wire_event("TEXT_MESSAGE_CONTENT", messageId="m1", delta="x")
            for _ in range(deltas)
        ],
        wire_event("TEXT_MESSAGE_END", messageId="m1"),
        wire_event("RUN_FINISHED", threadId="t1", runId="r1"),
    ]
    try:
        for event in events:
            await thread.append("event", runId="r1", event=event)
            assert (await anext(follower))["event"] == event
        with pytest.raises(StopAsyncIteration):
            await anext(follower)
    finally:
        await follower.aclose()
        adapter._runs.pop(thread.tape.name)
    print(f"{deltas=}: {time.perf_counter() - started:.3f}s {reads=} {projections=}")
    assert projections == 0
    assert reads == len(events)


@pytest.mark.parametrize("after", [0, 10000])
async def test_completed_run_replay_does_not_wait_for_another_run(setup, after):
    _, make = setup
    adapter = make()
    thread = await adapter.thread("alice", "t1")
    await thread.append("admit", runId="r1", fingerprint="test")
    terminal = wire_event("RUN_FINISHED", threadId="t1", runId="r1")
    await thread.append("event", runId="r1", event=terminal)
    await thread.append("admit", runId="r2", fingerprint="other")
    adapter._runs[thread.tape.name] = SimpleNamespace(run_id="r2")
    try:
        records = [r async for r in adapter.events(thread, "r1", after)]
    finally:
        adapter._runs.pop(thread.tape.name)
    assert [r["event"] for r in records] == ([] if after else [terminal])
