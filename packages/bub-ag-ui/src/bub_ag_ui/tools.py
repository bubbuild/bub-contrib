"""Native tools whose execution hands control back to the AG-UI client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from ag_ui.core import Tool as ClientTool
from bub.builtin.agent import BubAgent
from bub.core.events import ChatFinished, LLMCallFinished, ToolCallFinished
from bub.core.tape import Tape, TapeEntry
from bub.core.tools import Tool, ToolContext, tool
from bub.errors import BubError
from bub.streaming import StreamEvent
from bub.turn import TurnState

from bub_ag_ui.events import result_text


@dataclass(frozen=True)
class InterruptTool:
    """A model-callable human decision, resolved by RunAgentInput.resume."""

    name: str
    description: str
    parameters: dict[str, Any]
    response_schema: dict[str, Any] | None = None
    reason: str = "human_input"


class ExternalCallsPending(BaseException):
    """Control transfer, not a failed tool result or task cancellation.

    Bub's executor waits for the entire parallel batch before propagating this.
    Ordinary tool exceptions would instead be normalized into model-facing errors.
    Only an admitted native tool handler can request this transfer: deny/replace
    decisions never invoke the handler and therefore never create pending work.
    """


@dataclass
class ExternalTools:
    definitions: list[ClientTool | InterruptTool]
    pending: list[dict[str, Any]] = field(default_factory=list)
    context: ToolContext | None = None

    def validate(self, agent: BubAgent) -> None:
        names = {name.replace(".", "_") for name in agent.tools}
        for definition in self.definitions:
            name = definition.name.replace(".", "_")
            if name in names:
                raise ValueError(
                    f"Client/interrupt tool shadows an existing tool: {definition.name}"
                )
            names.add(name)

    def bind(self, agent: BubAgent, state: TurnState) -> None:
        # Repeat validation after load_state: plugins may bind session tools there.
        self.validate(agent)
        for definition in self.definitions:
            agent.tools[definition.name] = self._tool(definition, state)

    def _tool(self, definition: ClientTool | InterruptTool, state: TurnState) -> Tool:
        @tool(
            name=definition.name,
            description=definition.description,
            context=True,
            exposure="direct",
        )
        def suspend(*, context: ToolContext, **arguments: Any) -> None:
            if context.state is not state:
                raise ValueError(
                    "Frontend/interrupt tools belong to the owning AG-UI turn"
                )
            self.context = context
            call = {
                "id": context.call_id,
                "name": definition.name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
                "kind": "frontend",
            }
            if isinstance(definition, InterruptTool):
                call.update(
                    kind="interrupt",
                    reason=definition.reason,
                    response_schema=definition.response_schema,
                )
            self.pending.append(call)
            raise ExternalCallsPending()

        # Client schemas are supplied at runtime, rather than by Python annotations.
        return replace(suspend, parameters=definition.parameters)

    async def record_pause(
        self,
        agent: BubAgent,
        tape: Tape,
        completed: LLMCallFinished | None,
        results: dict[str, ToolCallFinished],
        system_prompt: str | None,
        reasoning: list[str],
    ) -> StreamEvent | None:
        """Record only the exchange interrupted by an admitted external call.

        Normal exchanges are recorded by run_once. Its tool executor settles
        the whole batch before propagating our control transfer, so completed
        backend results are available here without fabricating pending results.
        """
        if completed is None or self.context is None:
            raise RuntimeError("External call without a model exchange")
        request, response = completed.request, completed.result
        await agent.events.emit(
            ChatFinished(
                state=tape.context.state,
                tape=tape,
                run_id=request.run_id,
                model=request.model,
                system_prompt=system_prompt,
                new_messages=self.context.step_messages or [],
                response_text=response.text,
                tool_calls=response.tool_calls,
                usage=response.usage,
                assistant_fields={"reasoning": "".join(reasoning)} if reasoning else {},
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
                        if value is not None and isinstance(result.error, BubError)
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
        return (
            StreamEvent("tool_result", {"call_ids": ids, "tool_results": values})
            if values
            else None
        )
