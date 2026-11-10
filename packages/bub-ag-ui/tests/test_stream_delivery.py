"""Live delivery must never poll a store or grow an unbounded event buffer."""

import asyncio
from contextlib import aclosing

import pytest
from bub import hookimpl
from bub.streaming import AsyncStreamEvents, StreamEvent

from .conftest import request


@pytest.mark.parametrize("storage", ["memory", "sqlite"])
@pytest.mark.parametrize("deltas", [400, 1600])
async def test_stream_store_reads_do_not_grow_with_tokens(
    setup, monkeypatch, tmp_path, deltas, storage
):
    _, make = setup
    from bub_tapestore_sqlite import SQLiteTapeStore

    sqlite = (
        SQLiteTapeStore(tmp_path / "stream.sqlite3") if storage == "sqlite" else None
    )
    adapter = make(tape_store=sqlite)
    reads = 0
    store = adapter._storage_agent.tape("probe").store
    fetch = store.fetch_all

    async def counted(query):
        nonlocal reads
        reads += 1
        return await fetch(query)

    monkeypatch.setattr(store, "fetch_all", counted)

    class Provider:
        @hookimpl(tryfirst=True)
        def run_model_stream(self):
            async def source():
                initial = reads
                for _ in range(deltas):
                    yield StreamEvent("text", {"delta": "x"})
                    assert reads == initial
                yield StreamEvent("final", {"ok": True})

            return AsyncStreamEvents(source())

    adapter.framework.plugin_manager.register(Provider(), "many-tokens")
    try:
        events = [e async for e in adapter.run("alice", request())]
    finally:
        if sqlite is not None:
            await sqlite.close()
    assert events[-1]["type"] == "RUN_FINISHED"
    assert sum(e["type"] == "TEXT_MESSAGE_CONTENT" for e in events) == deltas
    assert reads < 10


async def test_paused_consumer_has_bounded_delivery_and_can_close(setup):
    _, make = setup
    adapter = make()
    produced = 0
    closed = asyncio.Event()

    class Provider:
        @hookimpl(tryfirst=True)
        def run_model_stream(self):
            async def source():
                nonlocal produced
                try:
                    for _ in range(10000):
                        produced += 1
                        yield StreamEvent("text", {"delta": "x"})
                finally:
                    closed.set()

            return AsyncStreamEvents(source())

    adapter.framework.plugin_manager.register(Provider(), "backpressure")
    async with aclosing(adapter.run("alice", request())) as events:
        async for event in events:
            if event["type"] == "TEXT_MESSAGE_CONTENT":
                break
        await asyncio.sleep(0.05)
        assert produced < 40
    await asyncio.wait_for(closed.wait(), 5)
    assert not adapter._active
