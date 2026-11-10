"""Protocol turn orchestration over Bub's public, native step API.

AG-UI has a durable return-to-client boundary, unlike a continuous CLI loop.
BubAgent still owns model requests, policy hooks and the entire tool batch. No
agent method is overridden and no backend tool is executed by this module.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import TYPE_CHECKING

from bub.core.events import (
    ChatFinished,
    LLMCallFinished,
    MessagesPending,
    RunFinished,
    RunStarted,
    StepFinished,
    StepStarted,
    ToolCallFinished,
)
from bub.core.model import Model, is_context_length_error, model_client
from bub.core.system_prompt import PromptContext
from bub.core.tape import Tape, TapeEntry
from bub.errors import BubError
from bub.streaming import StreamEvent, StreamState

from bub_ag_ui.events import result_text
from bub_ag_ui.tools import ExternalCallsPending

if TYPE_CHECKING:
    from bub_ag_ui.execution import AGUIInboundMessage


async def run_steps(
    inbound: AGUIInboundMessage, tape: Tape, model: Model, stream: StreamState
) -> AsyncIterator[StreamEvent]:
    model = model_client(model)
    agent, external = inbound._runtime_agent, inbound.external
    state = tape.context.state
    completed: LLMCallFinished | None = None
    results: dict[str, ToolCallFinished] = {}

    def llm_finished(event: LLMCallFinished) -> None:
        nonlocal completed
        completed = event

    def tool_finished(event: ToolCallFinished) -> None:
        if event.call.context and event.call.context.call_id:
            results[event.call.context.call_id] = event

    agent.events.on(LLMCallFinished, llm_finished)
    agent.events.on(ToolCallFinished, tool_finished)
    started = time.monotonic()
    status, error, step = "cancelled", None, 0
    handoff_retries = 1
    try:
        tools = await agent.prepare_tools(tape, None)
        spec = model.spec
        stream.tools = tools
        stream.system_prompt = (
            await agent.system_prompt.render(
                PromptContext(
                    agent=agent, tape=tape, prompt=[], tools=tools, model=spec
                )
            )
            or None
        )
        await agent.events.emit(
            RunStarted(state=state, tape=tape, model=spec, prompt=[])
        )
        prompt, pending = None, []
        for step in range(1, agent.max_steps + 1):
            step_started = time.monotonic()
            step_status, step_error = "cancelled", None
            final = {}
            reasoning = []
            completed = None
            results.clear()
            await agent.events.emit(
                StepStarted(state=state, tape=tape, step=step, prompt=prompt)
            )
            try:
                async with aclosing(
                    agent.run_once(
                        prompt, tape=tape, model=model, state=stream, pending=pending
                    )
                ) as source:
                    try:
                        async for event in source:
                            if event.kind == "final":
                                final = event.data
                            elif event.kind == "reasoning":
                                reasoning.append(event.data["delta"])
                            yield event
                    except ExternalCallsPending:
                        # The native executor has completed all non-suspended calls,
                        # including deny/replace and after_tool_call hooks. Record a
                        # partial exchange, never fake a result for a pending call.
                        if completed is None or external.context is None:
                            raise RuntimeError(
                                "External call without a model exchange"
                            ) from None
                        request, response = completed.request, completed.result
                        await agent.events.emit(
                            ChatFinished(
                                state=state,
                                tape=tape,
                                run_id=request.run_id,
                                model=request.model,
                                system_prompt=stream.system_prompt,
                                new_messages=external.context.step_messages or [],
                                response_text=response.text,
                                tool_calls=response.tool_calls,
                                usage=stream.usage,
                                assistant_fields={"reasoning": "".join(reasoning)}
                                if reasoning
                                else {},
                            )
                        )
                        ids, values = [], []
                        for call in response.tool_calls:
                            if outcome := results.get(call["id"]):
                                result = outcome.result
                                value = result.result
                                if result.error is not None:
                                    value = (
                                        BubError(result.error.kind, result_text(value))
                                        if value is not None
                                        and isinstance(result.error, BubError)
                                        else result.error
                                    )
                                ids.append(call["id"])
                                values.append(value)
                                await tape.store.append(
                                    tape.name,
                                    TapeEntry.message(
                                        {
                                            "role": "tool",
                                            "tool_call_id": call["id"],
                                            "content": result_text(value),
                                        },
                                        run_id=request.run_id,
                                    ),
                                )
                        if values:
                            yield StreamEvent(
                                "tool_result", {"call_ids": ids, "tool_results": values}
                            )
                        await tape.append_event(
                            "ag_ui.suspended", {"pending": external.pending}
                        )
                        final = {"ok": True, "pending": True}
                        yield StreamEvent("final", final)
                pending = []
                continuing = bool(final.get("tool_calls") or final.get("tool_results"))
                if not continuing and not external.pending and step < agent.max_steps:
                    event = await agent.events.emit(
                        MessagesPending(
                            state=state, tape=tape, prompt=None, step_ended=True
                        )
                    )
                    pending = event.messages
                step_status = "continue" if continuing or pending else "ok"
            except Exception as exc:
                step_status, step_error = "error", str(exc)
                if handoff_retries and is_context_length_error(step_error):
                    handoff_retries -= 1
                    await tape.handoff(
                        name="auto_handoff/context_overflow",
                        state={
                            "reason": "context_length_exceeded",
                            "error": step_error,
                        },
                    )
                    step_status = "auto_handoff"
                    continue
                raise
            finally:
                finished = await agent.events.emit(
                    StepFinished(
                        state=state,
                        tape=tape,
                        step=step,
                        status=step_status,
                        elapsed_ms=int((time.monotonic() - step_started) * 1000),
                        error=step_error,
                        prompt=prompt,
                        stream=stream,
                    )
                )
            if external.pending or step_status == "ok":
                status = "ok"
                return
            prompt = finished.next_prompt
        raise RuntimeError(f"max_steps_reached={agent.max_steps}")
    except Exception as exc:
        status, error = "error", str(exc)
        raise
    finally:
        agent.events.off(LLMCallFinished, llm_finished)
        agent.events.off(ToolCallFinished, tool_finished)
        await agent.events.emit(
            RunFinished(
                state=state,
                tape=tape,
                status=status,
                steps=step,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=error,
            )
        )
