"""Explicit, JSON-only shared state for tools; never exposes Bub TurnState."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import jsonpatch
from ag_ui.core import BaseEvent
from bub.core.tools import ToolContext

from bub_ag_ui.events import Emit, wire_event


class SharedState:
    def __init__(self, value: Any, emit: Emit) -> None:
        self._value = deepcopy(value)
        self._emit = emit
        self._lock = asyncio.Lock()

    @property
    def value(self) -> Any:
        return deepcopy(self._value)

    async def set(self, value: Any) -> None:
        async with self._lock:
            event = wire_event("STATE_SNAPSHOT", snapshot=value)
            await self._emit(event)
            self._value = deepcopy(event["snapshot"])

    async def patch(self, operations: list[dict[str, Any]]) -> None:
        async with self._lock:
            updated = jsonpatch.apply_patch(self._value, operations)
            await self._emit(wire_event("STATE_DELTA", delta=operations))
            self._value = updated

    async def emit(self, event: BaseEvent) -> None:
        """Send application events, not lifecycle or message ownership events."""
        if event.type not in {"CUSTOM", "RAW", "ACTIVITY_SNAPSHOT", "ACTIVITY_DELTA"}:
            raise ValueError(
                "Use set/patch for state; run/message events belong to the adapter"
            )
        await self._emit(event.model_dump(mode="json", by_alias=True))


def shared_state(context: ToolContext) -> SharedState:
    """Access AG-UI state from an ordinary context-enabled Bub tool."""
    value = context.state.get("ag_ui_state")
    if not isinstance(value, SharedState):
        raise TypeError("This tool is not running in an AG-UI request")
    return value
