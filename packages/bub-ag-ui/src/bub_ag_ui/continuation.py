"""Validate live external calls and persist consumed interrupt receipts only."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from ag_ui.core import RunAgentInput
from bub.core.tape import Tape

from bub_ag_ui.events import result_text


@dataclass
class Continuation:
    pending: list[dict[str, Any]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    async def load(cls, tape: Tape) -> Continuation:
        entries = await tape.query().kinds("event").all()
        for entry in reversed(list(entries)):
            if entry.payload.get("name") == "ag_ui.continuation":
                return cls(
                    responses=deepcopy(entry.payload["data"].get("responses", []))
                )
        return cls()

    async def save(self, tape: Tape) -> None:
        await tape.append_event(
            "ag_ui.continuation",
            {"responses": deepcopy(self.responses)},
        )

    def prepare(self, request: RunAgentInput) -> tuple[list[dict[str, Any]], bool]:
        """Validate one complete continuation; repeated interrupt responses are no-ops."""
        messages = [m.model_dump(mode="json", by_alias=True) for m in request.messages]
        if len({m["id"] for m in messages}) != len(messages):
            raise ValueError("Duplicate message IDs")
        responses = [
            r.model_dump(mode="json", by_alias=True) for r in request.resume or []
        ]
        # Metadata is not the human decision. Idempotency uses the protocol's
        # (threadId, interruptId, status, payload) key, not an entire HTTP request.
        responses = [
            {k: r.get(k) for k in ("interruptId", "status", "payload")}
            for r in responses
        ]
        responses.sort(key=lambda r: r["interruptId"])
        repeated = bool(
            responses
            and not self.pending
            and json.dumps(responses, sort_keys=True)
            == json.dumps(self.responses, sort_keys=True)
        )
        if responses and not self.pending and not repeated:
            raise ValueError("Unknown or already resolved interrupt")
        interrupts = {c["id"]: c for c in self.pending if c["kind"] == "interrupt"}
        if len({r["interruptId"] for r in responses}) != len(responses):
            raise ValueError("Duplicate interrupt response")
        if not repeated and {r["interruptId"] for r in responses} != interrupts.keys():
            raise ValueError("Resume must address every pending interrupt exactly once")
        for pending in self.pending:
            results = [
                m
                for m in messages
                if m["role"] == "tool" and m["toolCallId"] == pending["id"]
            ]
            if pending["kind"] == "frontend" and len(results) != 1:
                raise ValueError("Provide one result for every pending frontend tool")
            if pending["kind"] == "interrupt" and results:
                raise ValueError(
                    "Use resume, not a tool message, to resolve an interrupt"
                )
        for response in responses:
            call = interrupts.get(
                response["interruptId"], {"id": response["interruptId"]}
            )
            if repeated and any(m.get("toolCallId") == call["id"] for m in messages):
                continue
            if response["status"] == "resolved" and call.get("response_schema"):
                jsonschema.validate(response["payload"], call["response_schema"])
            # Normalize the decision into a tool value for the waiting call.
            # Native tool execution emits the result; this is not a UI snapshot.
            index = next(
                (
                    i + 1
                    for i, m in enumerate(messages)
                    if any(c["id"] == call["id"] for c in m.get("toolCalls", []))
                ),
                0,
            )
            while index < len(messages) and messages[index]["role"] == "tool":
                index += 1
            messages.insert(
                index,
                {
                    "id": "resume:" + call["id"],
                    "role": "tool",
                    "toolCallId": call["id"],
                    "content": result_text(
                        response["payload"]
                        if response["status"] == "resolved"
                        else {"cancelled": True}
                    ),
                },
            )
        if len({m["id"] for m in messages}) != len(messages):
            raise ValueError("Message ID conflicts with an interrupt result")
        self.responses = responses or self.responses
        self.pending = []
        return messages, repeated
