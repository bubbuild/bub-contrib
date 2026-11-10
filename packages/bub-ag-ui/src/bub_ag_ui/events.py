"""Typed stream events and their AG-UI observers."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import jsonpatch
from ag_ui.core import Event as AGUIEvent
from bub.core.events import Event
from bub.errors import BubError
from bub.streaming import StreamEvent
from pydantic import TypeAdapter

EVENT = TypeAdapter(AGUIEvent)
type Emit = Callable[[dict[str, Any]], Awaitable[None]]


def wire_event(type: str, **fields: Any) -> dict[str, Any]:
    return EVENT.validate_python({"type": type, **fields}).model_dump(
        mode="json", by_alias=True
    )


def result_text(value: Any) -> str:
    if isinstance(value, BubError):
        value = value.as_dict()
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


class MessageProjection:
    """Current response messages for activity validation and interrupt snapshots."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

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
        self._emit = emit
        self._messages = MessageProjection()
        self.message_id: str | None = None
        self.reasoning_id: str | None = None

    async def send(self, type: str, **fields: Any) -> None:
        await self.publish(wire_event(type, **fields))

    async def publish(self, event: dict[str, Any]) -> None:
        self._messages.apply(event)
        await self._emit(event)

    async def snapshot(self) -> None:
        await self.send("MESSAGES_SNAPSHOT", messages=self._messages.messages)

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


@dataclass(kw_only=True)
class StreamOutput(Event):
    """A stream chunk published on the agent bus, scoped to its execution.

    These are not native AgentEvents: lifecycle/recording observers must not
    mistake a protocol stream chunk for another model or tool execution.
    """

    owner: object
    output: EventStream
    data: dict[str, Any]


class TextDelta(StreamOutput):
    pass


class ReasoningDelta(StreamOutput):
    pass


class ToolCalls(StreamOutput):
    pass


class ToolResults(StreamOutput):
    pass


class Usage(StreamOutput):
    pass


class StreamError(StreamOutput):
    pass


class StepFinal(StreamOutput):
    pass


async def text_delta(event: TextDelta) -> None:
    if delta := event.data.get("delta"):
        output = event.output
        await output.close_reasoning()
        await output.send(
            "TEXT_MESSAGE_CONTENT", messageId=await output.text_start(), delta=delta
        )


async def reasoning_delta(event: ReasoningDelta) -> None:
    if delta := event.data.get("delta"):
        output = event.output
        if output.reasoning_id is None:
            output.reasoning_id = str(uuid4())
            await output.send("REASONING_START", messageId=output.reasoning_id)
            await output.send(
                "REASONING_MESSAGE_START",
                messageId=output.reasoning_id,
                role="reasoning",
            )
        await output.send(
            "REASONING_MESSAGE_CONTENT", messageId=output.reasoning_id, delta=delta
        )


async def tool_calls(event: ToolCalls) -> None:
    output = event.output
    await output.close_reasoning()
    parent = await output.text_start()
    for call in event.data["tool_calls"]:
        await output.send(
            "TOOL_CALL_START",
            toolCallId=call["id"],
            toolCallName=call["name"],
            parentMessageId=parent,
        )
        args = call["arguments"]
        await output.send(
            "TOOL_CALL_ARGS",
            toolCallId=call["id"],
            delta=args
            if isinstance(args, str)
            else json.dumps(args, ensure_ascii=False),
        )
        await output.send("TOOL_CALL_END", toolCallId=call["id"])


async def tool_results(event: ToolResults) -> None:
    for call_id, result in zip(
        event.data["call_ids"], event.data["tool_results"], strict=True
    ):
        await event.output.send(
            "TOOL_CALL_RESULT",
            messageId=str(uuid4()),
            toolCallId=call_id,
            content=result_text(result),
            role="tool",
        )


async def usage(event: Usage) -> None:
    await event.output.send("CUSTOM", name="bub.usage", value=event.data)


async def stream_error(event: StreamError) -> None:
    raise RuntimeError(event.data.get("message", "Agent stream failed"))


async def step_final(event: StepFinal) -> None:
    await event.output.close()
    if not event.data.get("ok", True):
        raise RuntimeError("Agent step failed")


STREAM_OBSERVERS = {
    "text": (TextDelta, text_delta),
    "reasoning": (ReasoningDelta, reasoning_delta),
    "tool_call": (ToolCalls, tool_calls),
    "tool_result": (ToolResults, tool_results),
    "usage": (Usage, usage),
    "error": (StreamError, stream_error),
    "final": (StepFinal, step_final),
}


def stream_event(
    event: StreamEvent, owner: object, output: EventStream | None
) -> StreamOutput:
    """Bridge only: preserve the native chunk; leave adaptation to bus observers."""
    if output is None:
        raise RuntimeError(
            "Native execution produced output without an attached request"
        )
    event_type, _ = STREAM_OBSERVERS[event.kind]
    return event_type(owner=owner, output=output, data=event.data)
