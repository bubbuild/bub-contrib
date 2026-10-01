"""Lody subagent execution events (``_lody/subagents/event``).

Bub's built-in ``subagent`` tool consumes the child agent's stream internally, so
an ACP client only ever sees the outer tool call. When the client negotiates
``_meta.lody.subagentEvents``, the ACP server replaces the runtime agent's
``subagent`` tool with a streaming variant that republishes the child run through
the Lody contract: one ``snapshot`` before the run, sparse ``progress``
observations, one ``output`` event per child stream chunk, and a terminal
``snapshot``.

The contract is deliberately lossy: there are no sequence numbers, no replay
guarantees and no cross-reconnect deduplication, and a dropped event never fails
the tool. Publishing is best effort because the tool result, not the event
stream, is what the model depends on.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from acp.interfaces import Client
from acp.schema import ClientCapabilities

logger = logging.getLogger(__name__)

LODY_SUBAGENT_EVENT_METHOD = "lody/subagents/event"
LODY_SUBAGENT_EVENTS_CAPABILITY: dict[str, int] = {"version": 1}

#: Stream kinds this adapter republishes from a child run. Bub's agent loop has no
#: plan updates, so ``plan`` is not advertised.
STREAMED_OUTPUT_KINDS = ("text", "thought", "tool")

TERMINAL_SUBAGENT_STATES = ("completed", "failed", "cancelled")

_SUBAGENT_SUPPORT: dict[str, object] = {
    "stream": list(STREAMED_OUTPUT_KINDS),
    "progress": True,
    # Output is pushed live; runs are not addressable through the separate
    # `_lody/subagents/list|cancel|output` request surface.
    "outputRead": "none",
    "cancel": False,
}


def client_supports_subagent_events(
    capabilities: ClientCapabilities | Mapping[str, object] | None,
) -> bool:
    """Whether the client negotiated ``_meta.lody.subagentEvents`` version 1.

    Both sides opt in independently, so the agent may only publish events after
    the client advertised the same version.
    """

    events = _lody_capability(capabilities, "subagentEvents")
    return events is not None and events.get("version") == 1


def _lody_capability(
    capabilities: ClientCapabilities | Mapping[str, object] | None,
    name: str,
) -> Mapping[str, object] | None:
    meta = _field(capabilities, "field_meta") or _field(capabilities, "_meta")
    if not isinstance(meta, Mapping):
        return None
    lody = meta.get("lody")
    if not isinstance(lody, Mapping):
        return None
    capability = lody.get(name)
    return capability if isinstance(capability, Mapping) else None


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def json_safe(value: object) -> object:
    """Return a JSON-serializable view of a tool payload for the wire.

    Child tool arguments normally arrive as parsed JSON, but a provider may hand
    back a live object instead. Event payloads must survive serialization, so fall
    back to ``repr`` rather than dropping the event.
    """

    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


class SubagentEventEmitter:
    """Publishes one subagent execution through ``_lody/subagents/event``.

    One instance owns one opaque ``runId``. Snapshots replace previously known
    task metadata, so the emitter only has to track its own lifetime.
    """

    def __init__(self, client: Client, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._started_at = time.time()
        self._terminated = False
        self.run_id = uuid4().hex

    async def started(
        self, *, description: str | None = None, model_id: str | None = None
    ) -> None:
        await self.snapshot(
            "running",
            description=description,
            model_id=model_id,
            started_at=self._started_at,
        )

    async def snapshot(
        self,
        state: str,
        *,
        description: str | None = None,
        model_id: str | None = None,
        summary: str | None = None,
        started_at: float | None = None,
        reason_code: str | None = None,
        reason_message: str | None = None,
    ) -> None:
        """Publish a task snapshot; a terminal state ends this emitter."""

        if self._terminated:
            return
        if state in TERMINAL_SUBAGENT_STATES:
            self._terminated = True
        reason = (
            {"code": reason_code, "message": reason_message} if reason_code else None
        )
        await self._publish(
            {
                "type": "snapshot",
                "snapshot": _prune(
                    {
                        "state": state,
                        "description": description,
                        "modelId": model_id,
                        "summary": summary,
                        "startedAtEpochSeconds": _epoch_seconds(started_at),
                        "endedAtEpochSeconds": (
                            int(time.time())
                            if state in TERMINAL_SUBAGENT_STATES
                            else None
                        ),
                        "reason": reason,
                        "support": dict(_SUBAGENT_SUPPORT),
                    }
                ),
            }
        )

    async def output(self, update: Mapping[str, object]) -> None:
        """Publish one child stream chunk as an ACP session-update variant."""

        if self._terminated:
            return
        await self._publish({"type": "output", "update": dict(update)})

    async def progress(self, **fields: Any) -> None:
        """Publish a sparse absolute progress observation."""

        if self._terminated:
            return
        await self._publish({"type": "progress", "progress": _prune(fields)})

    async def _publish(self, event: Mapping[str, object]) -> None:
        payload = _prune(
            {
                "version": 1,
                "sessionId": self._session_id,
                "runId": self.run_id,
                **event,
            }
        )
        try:
            await self._client.ext_notification(LODY_SUBAGENT_EVENT_METHOD, payload)
        except Exception:
            # Observing a run must never break the run itself.
            logger.warning("Failed to publish a subagent event", exc_info=True)


def _prune(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _epoch_seconds(value: float | None) -> int | None:
    return None if value is None else int(value)
