"""AG-UI history mounted on the owning Bub tape, like the spill sidecar.

The tape is the source of truth. Views are derived on read; there is no separate
journal, sequence counter, or persistent projection/index. Protocol writers use
the canonical owner tape, not the model's temporary execution fork.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from bub.core.tape import Tape, TapeEntry, TapeQuery

from bub_ag_ui.events import Projection


@dataclass
class HistoryView:
    projection: Projection = field(default_factory=Projection)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)
    model_tape: str = ""
    cursor: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages": self.projection.messages,
            "state": self.projection.state,
            "pending": self.pending,
            "runs": self.runs,
            "cursor": self.cursor,
        }


@dataclass(frozen=True)
class AGUIHistory:
    name: str = "ag-ui"

    async def entries(self, tape: Tape) -> list[TapeEntry]:
        return (
            await TapeQuery(tape=tape.sidecar_tape_name(self.name), store=tape.store)
            .kinds("event")
            .all()
        )

    async def read(self, tape: Tape) -> HistoryView:
        view = HistoryView(model_tape=tape.name)
        for entry in await self.entries(tape):
            view.cursor = entry.id
            data = deepcopy(entry.payload.get("data", {}))
            match entry.payload.get("name"):
                case "ag_ui.admit":
                    view.runs[data["runId"]] = {
                        "fingerprint": data["fingerprint"],
                        "done": False,
                    }
                case "ag_ui.checkpoint":
                    view.pending = data["pending"]
                    view.model_tape = data["model_tape"]
                case "ag_ui.event":
                    event = data["event"]
                    view.projection.apply(event)
                    if event["type"] in {"RUN_FINISHED", "RUN_ERROR"}:
                        view.runs[data["runId"]].update(done=True, outcome=event)
        return view

    async def append(self, tape: Tape, kind: str, **data: Any) -> None:
        # Only application patches need a trial application: an invalid path
        # must not poison persisted history. Other events use SDK validation.
        if kind == "event" and data["event"]["type"] in {
            "STATE_DELTA",
            "ACTIVITY_DELTA",
        }:
            view = await self.read(tape)
            view.projection.apply(deepcopy(data["event"]))
        await tape.store.append(
            tape.sidecar_tape_name(self.name),
            TapeEntry.event(f"ag_ui.{kind}", deepcopy(data)),
        )

    async def events(
        self, tape: Tape, run_id: str, after: int = 0
    ) -> list[dict[str, Any]]:
        return [
            {"sequence": entry.id, **deepcopy(entry.payload["data"])}
            for entry in await self.entries(tape)
            if entry.id > after
            and entry.payload.get("name") == "ag_ui.event"
            and entry.payload["data"].get("runId") == run_id
        ]
