"""Cancellation boundaries for writes and native tape-fork cleanup."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from typing import Any


async def committed[T](operation: Awaitable[T]) -> T:
    """Do not abandon a write still running in an executor on cancellation.

    The caller remains cancelled, but only after the operation has settled. In
    particular, a tape append cannot land *after* recovery has read the store.
    """
    task = asyncio.ensure_future(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


@asynccontextmanager
async def tape_session(agent: Any, name: str, state: dict[str, Any]):
    """Close/merge the native fork even if cancellation arrives during merge."""
    session = agent.session(name, state=state)
    tape = await session.__aenter__()
    try:
        yield tape
    except BaseException:
        suppressed = await committed(session.__aexit__(*sys.exc_info()))
        if not suppressed:
            raise
    else:
        await committed(session.__aexit__(None, None, None))
