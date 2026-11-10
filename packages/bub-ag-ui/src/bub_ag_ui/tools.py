"""Native tools whose execution hands control back to the AG-UI client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any

from ag_ui.core import Tool as ClientTool
from bub.builtin.agent import BubAgent
from bub.core.tools import Tool, ToolContext, tool
from bub.turn import TurnState


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
