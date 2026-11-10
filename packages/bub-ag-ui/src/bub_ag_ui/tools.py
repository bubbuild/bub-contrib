"""Native tools whose execution hands control back to the AG-UI client."""

from __future__ import annotations

import asyncio
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


@dataclass
class ExternalTools:
    definitions: list[ClientTool | InterruptTool]
    pending: list[dict[str, Any]] = field(default_factory=list)
    futures: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    batch: dict[str, str] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    paused: asyncio.Event = field(default_factory=asyncio.Event)

    def check_pause(self) -> None:
        if self.pending and self.batch.keys() <= self.completed | self.futures.keys():
            self.paused.set()

    def resolve(self, values: dict[str, Any]) -> None:
        self.paused.clear()
        self.pending.clear()
        for call_id, value in values.items():
            self.futures.pop(call_id).set_result(value)

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
        async def suspend(*, context: ToolContext, **arguments: Any) -> Any:
            if context.state is not state:
                raise ValueError(
                    "Frontend/interrupt tools belong to the owning AG-UI turn"
                )
            if (
                self.batch.get(context.call_id, "").replace(".", "_")
                != definition.name.replace(".", "_")
                or context.command
                or context.code_mode
            ):
                raise ValueError("External tools require a direct model tool call")
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
            future = asyncio.get_running_loop().create_future()
            self.futures[context.call_id] = future
            self.check_pause()
            return await future

        # Client schemas are supplied at runtime, rather than by Python annotations.
        return replace(suspend, parameters=definition.parameters)
