"""Cancellation boundaries for writes and native tape-fork cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


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
