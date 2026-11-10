"""Request-scoped AG-UI execution over Bub; no conversation-management service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import anyio
import jsonschema
from ag_ui.core import RunAgentInput
from bub.builtin.agent import BubAgent
from bub.core.tape import Tape, session_tape_name, to_message
from bub.prompt import to_content

from bub_ag_ui.continuation import Continuation
from bub_ag_ui.events import EventStream, result_text
from bub_ag_ui.execution import (
    AGUIInboundMessage,
    NativeExecution,
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


def model_message(message: dict[str, Any]) -> dict[str, Any]:
    role = message["role"]
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


def input_digest(message: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(message, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


async def new_inputs(
    tape: Tape, messages: list[dict[str, Any]], accepted: dict[str, str]
) -> list[tuple[str, dict[str, Any]]]:
    """Deduplicate protocol input without importing client history into Bub."""
    known = dict(accepted)
    for entry in await tape.query().kinds("event").all():
        if entry.payload.get("name") == "ag_ui.inputs":
            known.update(entry.payload["data"])
    additions = []
    for message in messages:
        if message["role"] != "user":
            continue
        converted = model_message(message)
        if message["id"] in known:
            if known[message["id"]] != input_digest(converted):
                raise ProtocolError(
                    "Editing accepted input is not supported; start a new thread"
                )
            continue
        additions.append((message["id"], converted))
    return additions


class AGUIAdapter:
    """Adapt a request and its native execution to an AG-UI event stream.

    Bub owns the conversation tape, context, and entire model loop. An HTTP
    request attaches to a live native turn; external tools wait inside that turn.
    Only input/interrupt receipts are adapter-owned, never model exchanges.
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
        self._turns: dict[str, NativeExecution] = {}

    def session_id(self, principal: str, thread_id: str) -> str:
        identity = json.dumps([principal, thread_id], ensure_ascii=False)
        return "ag-ui:" + hashlib.sha256(identity.encode()).hexdigest()

    async def run(
        self, principal: str, request: RunAgentInput
    ) -> AsyncIterator[dict[str, Any]]:
        # Bounded delivery, not a replay journal. Only an explicitly paused
        # native turn may outlive this HTTP attachment.
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
                        with anyio.CancelScope(shield=True):
                            await asyncio.gather(next_event, return_exceptions=True)
            await producer
        finally:
            disconnected = True
            with anyio.CancelScope(shield=True):
                if not producer.done() and not producer.cancelling():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)
                self._tasks.discard(producer)

    async def _run_native(self, execution: NativeExecution) -> None:
        inbound = execution.inbound
        try:
            async with asyncio.timeout(self.max_run_seconds) as timeout:
                execution.timeout = timeout
                execution.deadline = timeout.when()
                await self.framework.process_inbound(inbound, stream_output=True)
                if not inbound.stream_complete:
                    raise RuntimeError(
                        "AG-UI stream did not complete; check channel configuration"
                    )
                # Commands and custom model hooks need not emit ChatFinished.
                if execution.unrecorded:
                    tape = inbound._runtime_agent.tape(inbound.model_tape)
                    await committed(
                        tape.append_event("ag_ui.inputs", dict(execution.unrecorded))
                    )
                    execution.unrecorded.clear()
        finally:
            execution.unbind()

    def _start(self, session_id: str, execution: NativeExecution) -> None:
        self._turns[session_id] = execution
        execution.task = asyncio.create_task(self._run_native(execution))
        self._tasks.add(execution.task)

        def finished(task: asyncio.Task[None]) -> None:
            self._tasks.discard(task)
            if self._turns.get(session_id) is execution:
                del self._turns[session_id]
            if not task.cancelled():
                task.exception()  # A detached timeout still has its exception observed.

        execution.task.add_done_callback(finished)

    async def _execute(self, principal: str, request: RunAgentInput, emit: Any) -> None:
        stream = EventStream(emit)
        session_id = self.session_id(principal, request.thread_id)
        acquired = False
        execution: NativeExecution | None = None
        detached = False
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
            tape = self._storage_agent.tape(
                session_tape_name(session_id, self.workspace)
            )
            live = self._turns.get(session_id)
            continuation = await Continuation.load(tape)
            # Persisted receipts do not resurrect a suspended coroutine.
            continuation.pending = list(live.inbound.external.pending) if live else []
            try:
                messages, repeated = continuation.prepare(request)
                additions = await new_inputs(
                    tape, messages, live.accepted if live else {}
                )
                if repeated and additions:
                    raise ProtocolError(
                        "Send new input without an already resolved resume"
                    )
                if live:
                    if not live.inbound.external.paused.is_set():
                        raise ProtocolError(
                            "Another request is executing on this thread"
                        )
                    if request.tools != live.inbound.request.tools:
                        raise ProtocolError(
                            "Tool definitions cannot change while a native turn is waiting"
                        )
                elif not repeated:
                    if not additions or (
                        request.messages and request.messages[-1].role == "tool"
                    ):
                        raise ProtocolError(
                            "No new user input or live external call; waiting calls cannot survive a server restart"
                        )
                external = (
                    live.inbound.external
                    if live
                    else ExternalTools([*request.tools, *self.interrupts])
                )
                agent = (
                    live.inbound._runtime_agent
                    if live
                    else self.agent_factory(self.framework, tape_store=tape.store)
                )
                values = {
                    m["toolCallId"]: model_message(m)["content"]
                    for m in messages
                    if m["role"] == "tool" and m["toolCallId"] in external.futures
                }
                if not live:
                    external.validate(agent)
                    for definition in external.definitions:
                        jsonschema.Draft202012Validator.check_schema(
                            definition.parameters
                        )
            except (
                ValueError,
                jsonschema.ValidationError,
                jsonschema.SchemaError,
            ) as exc:
                raise ProtocolError(str(exc)) from exc
            await stream.send("MESSAGES_SNAPSHOT", messages=messages)
            if repeated:
                await stream.send(
                    "STATE_SNAPSHOT",
                    snapshot=request.state if request.state is not None else {},
                )
            else:
                receipts = {key: input_digest(value) for key, value in additions}
                if live:
                    execution = live
                    execution.stream = stream
                    execution.state["ag_ui_context"] = [
                        item.model_dump(mode="json") for item in request.context
                    ]
                    execution.state["ag_ui_forwarded_props"] = request.forwarded_props
                    execution.accepted.update(receipts)
                    execution.queued_receipts.update(receipts)
                    execution.queued.extend(
                        to_content(value["content"]) for _, value in additions
                    )
                    shared = execution.inbound.shared
                    await shared.set(request.state if request.state is not None else {})
                    execution.supplied = set(values)
                    if request.resume:
                        await committed(continuation.save(tape))
                    external.resolve(values)
                else:

                    async def publish(event: dict[str, Any]) -> None:
                        output = stream if execution is None else execution.stream
                        if output is not None:
                            await output.publish(event)

                    shared = SharedState(
                        request.state if request.state is not None else {}, publish
                    )
                    await shared.set(shared.value)
                    prompt = [
                        part
                        for _, value in additions
                        for part in to_content(value["content"])
                    ]
                    inbound = AGUIInboundMessage(
                        session_id=session_id,
                        channel="ag-ui",
                        chat_id=request.thread_id,
                        content="\n".join(
                            part for part in prompt if isinstance(part, str)
                        ),
                        is_active=True,
                        _runtime_agent=agent,
                        request=request,
                        model_tape=tape.name,
                        workspace=self.workspace,
                        shared=shared,
                        external=external,
                        prompt=prompt,
                        execution=None,
                    )
                    execution = NativeExecution(inbound, receipts, stream)
                    inbound.execution = execution
                    self._start(session_id, execution)
                waiter = asyncio.create_task(external.paused.wait())
                try:
                    await asyncio.wait(
                        (execution.task, waiter), return_when=asyncio.FIRST_COMPLETED
                    )
                    if execution.task.done():
                        await execution.task
                    else:
                        continuation.pending = list(external.pending)
                finally:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
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
                await execution.inbound.shared.set(execution.inbound.shared.value)
                fields["outcome"] = {"type": "interrupt", "interrupts": interrupts}
            await stream.send(
                "RUN_FINISHED",
                threadId=request.thread_id,
                runId=request.run_id,
                **fields,
            )
            detached = bool(continuation.pending)
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
            if execution is not None:
                execution.stream = None
                if not detached and not execution.task.done():
                    if not execution.finishing and not execution.task.cancelling():
                        execution.task.cancel()
                    await asyncio.gather(execution.task, return_exceptions=True)
            if acquired:
                self._active.discard(session_id)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks)
        merging = {turn.task for turn in self._turns.values() if turn.finishing}
        for task in tasks:
            if task not in merging and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
