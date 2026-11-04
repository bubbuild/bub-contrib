"""Lody subagent extensions for Bub's subagent tasks.

Bub publishes ``SubagentStarted``, ``SubagentOutput`` and ``SubagentFinished`` on the
runtime agent's event bus for every subagent task. The ACP server observes them and,
per negotiated capability:

- ``subagentEvents`` (``_lody/subagents/event``): one ``snapshot`` when the child starts,
  one ``output`` per child stream chunk, sparse ``progress`` observations and a terminal
  ``snapshot``.
- ``subagents``: the task's lifecycle as ``_meta.lody.task`` on the ``subagent`` tool
  call that started it, and the ``_lody/subagents/list|cancel|output`` requests. The
  opaque Lody run ID doubles as the task ID of those requests.

The event stream is deliberately lossy: there are no sequence numbers, no replay
guarantees and no cross-reconnect deduplication, and a dropped event never fails the
child run. Publishing is best effort because the tool result, not the event stream, is
what the model depends on.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp.interfaces import Client
from acp.schema import ClientCapabilities

if TYPE_CHECKING:
    from bub.builtin.subagents import SubagentTask

logger = logging.getLogger(__name__)

LODY_SUBAGENT_EVENT_METHOD = "lody/subagents/event"

#: Whether the installed Bub runs subagents as tasks that publish events.
BUB_SUBAGENT_TASKS = importlib.util.find_spec("bub.builtin.subagents") is not None
LODY_SUBAGENT_EVENTS_CAPABILITY: dict[str, int] = {"version": 1}

#: Stream kinds this adapter republishes from a child run. Bub's agent loop has no
#: plan updates, so ``plan`` is not advertised.
STREAMED_OUTPUT_KINDS = ("text", "thought", "tool")

TERMINAL_SUBAGENT_STATES = ("completed", "failed", "cancelled")

_SUBAGENT_SUPPORT: dict[str, object] = {
    "stream": list(STREAMED_OUTPUT_KINDS),
    "progress": True,
    # `_lody/subagents/output` returns the child's text so far, `cancel` stops it.
    "outputRead": "live_tail",
    "cancel": True,
}

LODY_SUBAGENTS_LIST_METHOD = "lody/subagents/list"
LODY_SUBAGENTS_CANCEL_METHOD = "lody/subagents/cancel"
LODY_SUBAGENTS_OUTPUT_METHOD = "lody/subagents/output"
LODY_SUBAGENTS_CAPABILITY: dict[str, object] = {
    "version": 1,
    "lifecycle": True,
    "list": True,
    "cancel": True,
    "output": True,
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
        self,
        *,
        description: str | None = None,
        model_id: str | None = None,
        name: str | None = None,
    ) -> None:
        await self.snapshot(
            "running",
            description=description,
            model_id=model_id,
            name=name,
            started_at=self._started_at,
        )

    async def snapshot(
        self,
        state: str,
        *,
        description: str | None = None,
        model_id: str | None = None,
        name: str | None = None,
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
            _prune({"code": reason_code, "message": reason_message})
            if reason_code
            else None
        )
        await self._publish(
            {
                "type": "snapshot",
                "snapshot": _prune(
                    {
                        "state": state,
                        "name": name,
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
    return {
        key: None if isinstance(value, _Null) else value
        for key, value in values.items()
        if value is not None
    }


def _epoch_seconds(value: float | None) -> int | None:
    return None if value is None else int(value)


@dataclass(eq=False)
class SubagentRun:
    """What the ACP server tracks about one Bub subagent task."""

    run_id: str
    session_id: str
    task: SubagentTask
    emitter: SubagentEventEmitter | None = None
    pending_tools: list[tuple[str, str]] = field(default_factory=list)
    next_tool_index: int = 0
    output: list[str] = field(default_factory=list)
    """The child's text so far, across steps, for ``_lody/subagents/output``."""
    reported_usage: object = None
    terminated: bool = False

    @property
    def live(self) -> bool:
        return not self.task.done


def task_status(task: SubagentTask) -> str:
    """The ``LodySubagentTask`` status of a Bub task."""

    if task.status == "cancelled":
        return "killed"
    if task.status == "failed":
        return "timed_out" if _timed_out(task) else "failed"
    if task.status == "completed":
        return "completed"
    return "running"


def snapshot_reason(task: SubagentTask) -> tuple[str, str | None] | None:
    """The terminal snapshot's ``reason`` code and message, if it did not complete."""

    if task.status == "cancelled":
        return "cancelled", None
    if task.status == "failed":
        message = "; ".join(task.errors) or None
        return ("timeout" if _timed_out(task) else "error"), message
    return None


def lody_task(run: SubagentRun) -> dict[str, Any]:
    """The ``LodySubagentTask`` entry of ``_lody/subagents/list``."""

    task = run.task
    started = task.started_at or task.created_at
    return _prune(
        {
            "taskId": run.run_id,
            "description": task.description or "subagent",
            "status": task_status(task),
            "subagentType": task.profile,
            "modelId": task.model,
            "startedAtEpochSeconds": int(started),
            # The contract spells out an explicit null for a running task.
            "endedAtEpochSeconds": int(task.ended_at) if task.ended_at else _NULL,
            "stopReason": task.status if task.done else None,
        }
    )


def lody_task_meta(run: SubagentRun, total_tokens: int | None) -> dict[str, Any]:
    """The ``LodyTaskMeta`` carried as ``_meta.lody.task`` on the starting tool call."""

    task = run.task
    if not task.done:
        status = "in_progress"
    else:
        status = "completed" if task.status == "completed" else "failed"
    started = task.started_at or task.created_at
    duration = (task.ended_at or time.time()) - started
    return _prune(
        {
            "version": 1,
            "taskId": run.run_id,
            "kind": "subagent",
            "status": status,
            "description": task.description or None,
            "actor": task.profile,
            "modelId": task.model,
            "startedAtEpochSeconds": int(started),
            "endedAtEpochSeconds": int(task.ended_at) if task.ended_at else None,
            "summary": summary_line(task.output) if task.done else None,
            "error": (
                ("; ".join(task.errors) or task.status)
                if task.status in ("failed", "cancelled")
                else None
            ),
            "lastToolName": task.last_tool,
            "usage": _prune(
                {
                    "totalTokens": total_tokens,
                    "toolUses": task.tool_calls,
                    "durationMs": int(duration * 1000),
                }
            ),
        }
    )


def summary_line(text: str) -> str | None:
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return line[:200] or None


def tail_lines(text: str, tail: int | None) -> str:
    if tail is None:
        return text
    return "\n".join(text.split("\n")[-tail:])


def _timed_out(task: SubagentTask) -> bool:
    return any(error.startswith("timed out after") for error in task.errors)


class _Null:
    """Marks a field whose explicit null the contract requires; ``_prune`` keeps it as None."""


_NULL: Any = _Null()
