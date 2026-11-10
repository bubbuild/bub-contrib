"""A deterministic projection from Bub streams to AG-UI events and messages."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from uuid import uuid4

import jsonpatch
from ag_ui.core import Event
from bub.errors import BubError
from bub.streaming import StreamEvent
from pydantic import TypeAdapter

EVENT = TypeAdapter(Event)
type Emit = Callable[[dict[str, Any]], Awaitable[None]]


def wire_event(type: str, **fields: Any) -> dict[str, Any]:
    return EVENT.validate_python({"type": type, **fields}).model_dump(
        mode="json", by_alias=True
    )


def result_text(value: Any) -> str:
    if isinstance(value, BubError):
        value = value.as_dict()
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class RunProjection:
    """Current response messages/state for validation and interrupt snapshots."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.state: Any = {}

    def message(self, id: str) -> dict[str, Any]:
        return next(message for message in self.messages if message["id"] == id)

    def apply(self, event: dict[str, Any]) -> None:
        match event["type"]:
            case "MESSAGES_SNAPSHOT":
                self.messages = deepcopy(event["messages"])
            case "TEXT_MESSAGE_START":
                self.messages.append(
                    {"id": event["messageId"], "role": event["role"], "content": ""}
                )
            case "TEXT_MESSAGE_CONTENT":
                self.message(event["messageId"])["content"] += event["delta"]
            case "REASONING_MESSAGE_START":
                self.messages.append(
                    {"id": event["messageId"], "role": "reasoning", "content": ""}
                )
            case "REASONING_MESSAGE_CONTENT":
                self.message(event["messageId"])["content"] += event["delta"]
            case "TOOL_CALL_START":
                parent = self.message(event["parentMessageId"])
                parent.setdefault("toolCalls", []).append(
                    {
                        "id": event["toolCallId"],
                        "type": "function",
                        "function": {"name": event["toolCallName"], "arguments": ""},
                    }
                )
            case "TOOL_CALL_ARGS":
                for message in self.messages:
                    for call in message.get("toolCalls", []):
                        if call["id"] == event["toolCallId"]:
                            call["function"]["arguments"] += event["delta"]
            case "TOOL_CALL_RESULT":
                self.messages.append(
                    {
                        "id": event["messageId"],
                        "role": "tool",
                        "content": event["content"],
                        "toolCallId": event["toolCallId"],
                    }
                )
            case "STATE_SNAPSHOT":
                self.state = deepcopy(event["snapshot"])
            case "STATE_DELTA":
                self.state = jsonpatch.apply_patch(self.state, event["delta"])
            case "ACTIVITY_SNAPSHOT":
                existing = next(
                    (m for m in self.messages if m["id"] == event["messageId"]), None
                )
                if existing is None:
                    self.messages.append(
                        {
                            "id": event["messageId"],
                            "role": "activity",
                            "activityType": event["activityType"],
                            "content": deepcopy(event["content"]),
                        }
                    )
                elif event.get("replace", True):
                    existing["activityType"] = event["activityType"]
                    existing["content"] = deepcopy(event["content"])
                # replace=False preserves the existing activity, not a merge.
            case "ACTIVITY_DELTA":
                message = self.message(event["messageId"])
                message["content"] = jsonpatch.apply_patch(
                    message["content"], event["patch"]
                )


class EventStream:
    """One run's wire state; a Bub final closes a step, never a run."""

    def __init__(self, emit: Emit) -> None:
        self.emit = emit
        self.message_id: str | None = None
        self.reasoning_id: str | None = None
        self.calls: list[dict[str, Any]] = []

    async def send(self, type: str, **fields: Any) -> None:
        await self.emit(wire_event(type, **fields))

    async def text_start(self) -> str:
        if self.message_id is None:
            self.message_id = str(uuid4())
            await self.send(
                "TEXT_MESSAGE_START", messageId=self.message_id, role="assistant"
            )
        return self.message_id

    async def close_reasoning(self) -> None:
        if self.reasoning_id is not None:
            await self.send("REASONING_MESSAGE_END", messageId=self.reasoning_id)
            await self.send("REASONING_END", messageId=self.reasoning_id)
            self.reasoning_id = None

    async def close(self) -> None:
        await self.close_reasoning()
        if self.message_id is not None:
            await self.send("TEXT_MESSAGE_END", messageId=self.message_id)
            self.message_id = None

    async def consume(self, event: StreamEvent) -> None:
        data = event.data
        match event.kind:
            case "text":
                if delta := data.get("delta"):
                    await self.close_reasoning()
                    await self.send(
                        "TEXT_MESSAGE_CONTENT",
                        messageId=await self.text_start(),
                        delta=delta,
                    )
            case "reasoning":
                if delta := data.get("delta"):
                    if self.reasoning_id is None:
                        self.reasoning_id = str(uuid4())
                        await self.send("REASONING_START", messageId=self.reasoning_id)
                        await self.send(
                            "REASONING_MESSAGE_START",
                            messageId=self.reasoning_id,
                            role="reasoning",
                        )
                    await self.send(
                        "REASONING_MESSAGE_CONTENT",
                        messageId=self.reasoning_id,
                        delta=delta,
                    )
            case "tool_call":
                await self.close_reasoning()
                parent = await self.text_start()
                self.calls = data["tool_calls"]
                for call in self.calls:
                    await self.send(
                        "TOOL_CALL_START",
                        toolCallId=call["id"],
                        toolCallName=call["name"],
                        parentMessageId=parent,
                    )
                    args = call["arguments"]
                    await self.send(
                        "TOOL_CALL_ARGS",
                        toolCallId=call["id"],
                        delta=args
                        if isinstance(args, str)
                        else json.dumps(args, ensure_ascii=False),
                    )
                    await self.send("TOOL_CALL_END", toolCallId=call["id"])
            case "tool_result":
                ids = data.get("call_ids", [call["id"] for call in self.calls])
                for call_id, result in zip(ids, data["tool_results"], strict=True):
                    await self.send(
                        "TOOL_CALL_RESULT",
                        messageId=str(uuid4()),
                        toolCallId=call_id,
                        content=result_text(result),
                        role="tool",
                    )
            case "usage":
                await self.send("CUSTOM", name="bub.usage", value=data)
            case "error":
                raise RuntimeError(data.get("message", "Agent stream failed"))
            case "final":
                await self.close()
                if not data.get("ok", True):
                    raise RuntimeError("Agent step failed")
