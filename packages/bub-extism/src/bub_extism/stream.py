from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from republic import AsyncStreamEvents, StreamEvent, StreamState


def stream_events_from_value(value: Any) -> AsyncStreamEvents | None:
    if value is None:
        return None

    events_value = value
    state = StreamState()
    if isinstance(value, dict):
        events_value = value.get("events", [])
        usage = value.get("usage")
        if isinstance(usage, dict):
            state.usage = usage

    if not isinstance(events_value, list):
        raise RuntimeError("Extism run_model_stream must return a list of stream events")

    events = [_stream_event_from_dict(item) for item in events_value]

    async def iterator() -> AsyncIterator[StreamEvent]:
        for event in events:
            yield event

    return AsyncStreamEvents(iterator(), state=state)


def _stream_event_from_dict(value: Any) -> StreamEvent:
    if not isinstance(value, dict):
        raise RuntimeError("Extism stream event must be a JSON object")
    kind = value.get("kind")
    data = value.get("data", {})
    if not isinstance(kind, str):
        raise RuntimeError("Extism stream event must include a string kind")
    if not isinstance(data, dict):
        raise RuntimeError("Extism stream event data must be a JSON object")
    return StreamEvent(kind, data)
