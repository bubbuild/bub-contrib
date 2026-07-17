from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState


def stream_events_from_value(value: Any) -> AsyncStreamEvents | None:
    if value is None:
        return None

    events, state = _stream_payload(value)

    async def iterator() -> AsyncIterator[StreamEvent]:
        for event in events:
            yield event

    return AsyncStreamEvents(iterator(), state=state)


def _stream_payload(value: Any) -> tuple[list[StreamEvent], StreamState]:
    state = StreamState()
    events_value = value
    if isinstance(value, dict):
        events_value = value.get("events", [])
        usage = value.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                raise RuntimeError(
                    "Extism run_model_stream usage must be a JSON object"
                )
            state.usage = usage

    if not isinstance(events_value, list):
        raise RuntimeError(
            "Extism run_model_stream must return a list of stream events"
        )
    return ([_stream_event_from_dict(item) for item in events_value], state)


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
