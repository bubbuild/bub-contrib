"""Request-scoped AG-UI execution over Bub; no conversation-management service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import jsonschema
from ag_ui.core import RunAgentInput
from bub.builtin.agent import BubAgent
from bub.core.tape import TapeEntry, session_tape_name, to_message

from bub_ag_ui.continuation import Continuation
from bub_ag_ui.events import EventStream, RunProjection, result_text
from bub_ag_ui.execution import (
    AGUIInboundMessage,
    StreamRouter,
    install_execution_hooks,
)
from bub_ag_ui.lifecycle import committed
from bub_ag_ui.state import SharedState
from bub_ag_ui.tools import ExternalTools, InterruptTool

logger = logging.getLogger(__name__)


class ProtocolError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def model_message(message: dict[str, Any]) -> dict[str, Any] | None:
    role = message["role"]
    if role not in {"user", "assistant", "tool"}:
        return None
    content = message.get("content", "")
    if isinstance(content, list):
        parts = []
        for part in content:
            if part["type"] == "text":
                parts.append({"type": "text", "text": part["text"]})
            elif part["type"] in {"image", "audio", "video"}:
                source = part.get("source", {})
                mime = part.get("mimeType") or source.get("mimeType")
                if not mime:
                    raise ProtocolError("Media input requires mimeType")
                data = {"type": part["type"], "media_type": mime}
                if source.get("type") == "url":
                    data["url"] = source["value"]
                elif source.get("type") == "data":
                    data["data"] = source["value"]
                else:
                    raise ProtocolError("Media input requires a URL or data source")
                parts.append(data)
            else:
                raise ProtocolError(f"Bub cannot consume {part['type']} input", 422)
        content = parts
    result = {"role": role, "content": content}
    if role == "assistant" and message.get("toolCalls"):
        result["tool_calls"] = deepcopy(message["toolCalls"])
    if role == "tool":
        result["tool_call_id"] = message["toolCallId"]
        if message.get("error"):
            result["content"] = result_text(
                {"error": message["error"], "content": content}
            )
    # Validate conversion before accepting the run, not after the first SSE byte.
    to_message(result)
    return result


class AGUIAdapter:
    """Adapt a request and its native execution to an AG-UI event stream.

    The client supplies conversation messages/state on every run. Only external
    call correlation and interrupt response receipts survive in the native tape.
    Streaming does not read back events from storage.
    """

    def __init__(
        self,
        framework: Any,
        *,
        workspace: Path | None = None,
        interrupts: tuple[InterruptTool, ...] = (),
        agent_factory: Callable[..., BubAgent] = BubAgent,
        max_run_seconds: float = 600,
        bind_router: bool = True,
    ) -> None:
        self.framework = framework
        install_execution_hooks(framework)
        if bind_router:
            framework.bind_channel_router(StreamRouter())
        self.workspace = (workspace or Path.cwd()).resolve()
        self.interrupts = interrupts
        self.agent_factory = agent_factory
        self.max_run_seconds = max_run_seconds
        self._storage_agent = agent_factory(framework)
        self._active: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def session_id(self, principal: str, thread_id: str) -> str:
        identity = json.dumps([principal, thread_id], ensure_ascii=False)
        return "ag-ui:" + hashlib.sha256(identity.encode()).hexdigest()

    async def run(
        self, principal: str, request: RunAgentInput
    ) -> AsyncIterator[dict[str, Any]]:
        # Bounded, transient delivery only. No event IDs, replay buffers, durable
        # wire journal, or global run registry. Closing this iterator owns cleanup.
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=16)
        disconnected = False

        async def emit(event: dict[str, Any]) -> None:
            task = asyncio.current_task()
            if not disconnected and not (task and task.cancelling()):
                await queue.put(event)

        producer = asyncio.create_task(self._execute(principal, request, emit))
        self._tasks.add(producer)
        try:
            while not producer.done() or not queue.empty():
                if not queue.empty():
                    yield queue.get_nowait()
                    continue
                next_event = asyncio.create_task(queue.get())
                try:
                    await asyncio.wait(
                        (producer, next_event), return_when=asyncio.FIRST_COMPLETED
                    )
                    if next_event.done():
                        yield next_event.result()
                finally:
                    if not next_event.done():
                        next_event.cancel()
                        await asyncio.gather(next_event, return_exceptions=True)
            await producer
        finally:
            disconnected = True
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            self._tasks.discard(producer)

    async def _execute(self, principal: str, request: RunAgentInput, emit: Any) -> None:
        # This projection lives only for this response: patches are validated and
        # an interrupt can publish the full resume context before RUN_FINISHED.
        projection = RunProjection()

        async def publish(event: dict[str, Any]) -> None:
            projection.apply(event)
            await emit(event)

        stream = EventStream(publish)
        session_id = self.session_id(principal, request.thread_id)
        acquired = False
        try:
            await stream.send(
                "RUN_STARTED",
                threadId=request.thread_id,
                runId=request.run_id,
                parentRunId=request.parent_run_id,
            )
            if self._closed:
                raise ProtocolError("Adapter is shutting down")
            if not principal or not request.thread_id or len(request.thread_id) > 512:
                raise ProtocolError("Invalid principal/threadId")
            if not request.run_id or len(request.run_id) > 512:
                raise ProtocolError("Invalid runId")
            if session_id in self._active:
                raise ProtocolError("Another request is executing on this thread")
            self._active.add(session_id)
            acquired = True
            async with asyncio.timeout(self.max_run_seconds):
                tape = self._storage_agent.tape(
                    session_tape_name(session_id, self.workspace)
                )
                continuation = await Continuation.load(tape)
                try:
                    had_pending = bool(continuation.pending)
                    messages, repeated = continuation.prepare(request)
                    agent = self.agent_factory(self.framework, tape_store=tape.store)
                    external = ExternalTools([*request.tools, *self.interrupts])
                    external.validate(agent)
                    for definition in external.definitions:
                        jsonschema.Draft202012Validator.check_schema(
                            definition.parameters
                        )
                    converted = [
                        value
                        for m in messages
                        if (value := model_message(m)) is not None
                    ]
                except (
                    ValueError,
                    jsonschema.ValidationError,
                    jsonschema.SchemaError,
                ) as exc:
                    raise ProtocolError(str(exc)) from exc
                await stream.send("MESSAGES_SNAPSHOT", messages=messages)
                shared = SharedState(
                    request.state if request.state is not None else {}, publish
                )
                await shared.set(shared.value)
                if not repeated:
                    # Each request supplies its context. A fresh native execution
                    # tape prevents server history from silently overriding or
                    # duplicating client messages; Bub still records model steps.
                    model_tape = tape.name + ":run:" + str(uuid4())
                    execution = agent.tape(model_tape)
                    await execution.ensure_bootstrap_anchor()
                    for message in converted:
                        await committed(
                            execution.store.append(
                                model_tape, TapeEntry.message(message)
                            )
                        )
                    if had_pending:
                        # Record receipt before executing any resumed side effect.
                        await committed(continuation.save(tape))
                    inbound = AGUIInboundMessage(
                        session_id=session_id,
                        channel="ag-ui",
                        chat_id=request.thread_id,
                        content=next(
                            (
                                m.get("content", "")
                                for m in reversed(messages)
                                if m["role"] == "user"
                                and isinstance(m.get("content"), str)
                            ),
                            "",
                        ),
                        is_active=True,
                        _runtime_agent=agent,
                        request=request,
                        model_tape=model_tape,
                        workspace=self.workspace,
                        shared=shared,
                        events=stream,
                        external=external,
                    )
                    await self.framework.process_inbound(inbound, stream_output=True)
                    if not inbound.stream_complete:
                        raise RuntimeError(
                            "AG-UI stream did not complete; check channel configuration"
                        )
                    continuation.pending = external.pending
                    if external.pending:
                        await committed(continuation.save(tape))
                await stream.close()
                interrupts = [
                    {
                        "id": c["id"],
                        "toolCallId": c["id"],
                        "reason": c["reason"],
                        "responseSchema": c.get("response_schema"),
                        "metadata": {
                            "arguments": json.loads(c["arguments"]),
                            "toolName": c["name"],
                        },
                    }
                    for c in continuation.pending
                    if c["kind"] == "interrupt"
                ]
                fields: dict[str, Any] = {}
                if interrupts:
                    await stream.send(
                        "MESSAGES_SNAPSHOT", messages=deepcopy(projection.messages)
                    )
                    await shared.set(shared.value)
                    fields["outcome"] = {"type": "interrupt", "interrupts": interrupts}
                await stream.send(
                    "RUN_FINISHED",
                    threadId=request.thread_id,
                    runId=request.run_id,
                    **fields,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("AG-UI request failed")
            await stream.close()
            message = (
                str(exc)
                if isinstance(exc, ProtocolError)
                else "Agent run failed; see server logs"
            )
            if isinstance(exc, TimeoutError):
                message = "Agent run timed out"
            await stream.send("RUN_ERROR", message=message, code="RUN_FAILED")
        finally:
            if acquired:
                self._active.discard(session_id)

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
