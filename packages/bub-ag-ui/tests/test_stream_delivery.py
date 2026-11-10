"""A slow client applies backpressure and can close its response."""

import asyncio
from contextlib import aclosing

from bub import hookimpl
from bub.streaming import AsyncStreamEvents, StreamEvent

from .conftest import request


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
        assert produced < 10000
    await asyncio.wait_for(closed.wait(), 5)
