"""Request-scoped AG-UI execution over Bub; no conversation-management service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import jsonschema
from ag_ui.core import RunAgentInput
from bub.builtin.agent import BubAgent
from bub.core.tape import Tape, TapeEntry, session_tape_name, to_message

from bub_ag_ui.continuation import Continuation
from bub_ag_ui.events import EventStream, result_text
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
    if role not in {"user", "tool"}:
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
    if role == "tool":
        result["tool_call_id"] = message["toolCallId"]
        if message.get("error"):
            result["content"] = result_text(
                {"error": message["error"], "content": content}
            )
    # Validate conversion before accepting the run, not after the first SSE byte.
    to_message(result)
    return result


async def new_inputs(
    tape: Tape, messages: list[dict[str, Any]], pending_ids: set[str]
) -> list[tuple[str, dict[str, Any]]]:
    """Map AG-UI input to native appends, never rebuild context from UI history.

    IDs on native input entries distinguish new messages from client echoes,
    including echoes of messages that Bub has already moved behind a handoff.
    Assistant output and backend tool results belong to the native recorder.
    """
    accepted = {
        entry.meta["ag_ui_message_id"]: entry.payload
        for entry in await tape.query().kinds("message").all()
        if "ag_ui_message_id" in entry.meta
    }
    additions = []
    for message in messages:
        if message["role"] != "user" and not (
            message["role"] == "tool" and message["toolCallId"] in pending_ids
        ):
            continue
        converted = model_message(message)
        if message["id"] in accepted:
            if accepted[message["id"]] != converted:
                raise ProtocolError(
                    "Editing accepted input is not supported; start a new thread"
                )
            continue
        additions.append((message["id"], converted))
    # Resolve the native pending exchange before any new user input sent with it.
    return sorted(additions, key=lambda item: item[1]["role"] != "tool")


class AGUIAdapter:
    """Adapt a request and its native execution to an AG-UI event stream.

    Bub owns the conversation tape and its context selection. The adapter appends
    only new user input or results for pending external calls. Client history is
    not a replacement for native context; streaming never polls stored events.
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
        stream = EventStream(emit)
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
                    pending_ids = {call["id"] for call in continuation.pending}
                    messages, repeated = continuation.prepare(request)
                    agent = self.agent_factory(self.framework, tape_store=tape.store)
                    external = ExternalTools([*request.tools, *self.interrupts])
                    external.validate(agent)
                    for definition in external.definitions:
                        jsonschema.Draft202012Validator.check_schema(
                            definition.parameters
                        )
                    additions = await new_inputs(tape, messages, pending_ids)
                    if repeated and additions:
                        raise ProtocolError(
                            "Send new input without an already resolved resume"
                        )
                    if not additions and not repeated:
                        raise ProtocolError("No new user input or pending tool result")
                except (
                    ValueError,
                    jsonschema.ValidationError,
                    jsonschema.SchemaError,
                ) as exc:
                    raise ProtocolError(str(exc)) from exc
                await stream.send("MESSAGES_SNAPSHOT", messages=messages)
                shared = SharedState(
                    request.state if request.state is not None else {}, stream.publish
                )
                await shared.set(shared.value)
                if not repeated:

                    async def append_input() -> None:
                        await tape.ensure_bootstrap_anchor()
                        if pending_ids:
                            # Record receipt before executing any resumed side effect.
                            await continuation.save(tape)
                        for message_id, message in additions:
                            await tape.store.append(
                                tape.name,
                                TapeEntry.message(message, ag_ui_message_id=message_id),
                            )

                    await committed(append_input())
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
                        model_tape=tape.name,
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
                    await stream.snapshot()
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
