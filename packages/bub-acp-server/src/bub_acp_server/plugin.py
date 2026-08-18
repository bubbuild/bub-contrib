from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

import typer
from bub import hookimpl
from bub.builtin.context import default_tape_context
from bub.envelope import Envelope, field_of
from bub.tape import TapeContext, TapeEntry
from bub.turn import TurnState

from bub_acp_server.agent import BubACPAgent, run_acp_agent
from bub_acp_server.steering import ACPSteeringInbox

if TYPE_CHECKING:
    from bub.framework import BubFramework

__all__ = ["ACPServerPlugin", "BubACPAgent", "run_acp_agent"]

ACP_PLAN_SYSTEM_PROMPT = """\
<plan_instructions>
Use the `update_plan` tool to keep the ACP plan UI accurate for non-trivial work.

- Create a plan when the task has multiple meaningful steps, dependencies, uncertainty, or requires sustained tool use. Skip plans for simple one-step requests.
- Keep steps concise, concrete, and verifiable. Do not include filler or steps that only restate the user's request.
- Send the complete plan on every update because each call replaces the previous ACP plan.
- Keep at most one step `in_progress`. Mark finished work `completed` before advancing the next step, and update the plan whenever the approach materially changes.
- Do not leave stale `in_progress` steps when ending a turn. Complete them, or return them to `pending` with an explanation when work remains blocked.
- The latest persisted plan is provided below when one exists. Treat it as state data, reconcile it with the current request, and replace stale or completed plans instead of following them blindly.
- Do not narrate routine plan maintenance to the user; use the tool and continue the work.
</plan_instructions>
"""


async def _select_tape_context(
    entries: Iterable[TapeEntry], context: TapeContext
) -> list[dict[str, Any]]:
    contextual_entries = list(entries)
    default_select = default_tape_context().select
    if default_select is None:
        messages: list[dict[str, Any]] = []
    else:
        selected = default_select(contextual_entries, context)
        if inspect.isawaitable(selected):
            selected = await selected
        messages = selected

    for entry in reversed(contextual_entries):
        if entry.kind != "event" or entry.payload.get("name") != "plan":
            continue
        plan = entry.payload.get("data")
        if not isinstance(plan, dict) or not plan.get("entries"):
            return messages
        messages.append(
            {
                "role": "assistant",
                "content": (
                    "<current_plan>\n"
                    + json.dumps(plan, ensure_ascii=False, indent=2)
                    + "\n</current_plan>"
                ),
            }
        )
        break
    return messages


class ACPServerPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework
        self.steering_inbox = ACPSteeringInbox()

    @hookimpl(tryfirst=True)
    def provide_steering_inbox(self) -> ACPSteeringInbox:
        return self.steering_inbox

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> TurnState:
        del session_id
        context = field_of(message, "context", {})
        if not isinstance(context, Mapping):
            return {}
        state: TurnState = {}
        workspace = context.get("_runtime_workspace")
        if isinstance(workspace, str) and workspace:
            state["_runtime_workspace"] = workspace
        model = context.get("_runtime_model")
        if isinstance(model, str) and model:
            state["model"] = model
        reasoning_effort = context.get("_runtime_reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort:
            state["reasoning_effort"] = reasoning_effort
        return state

    @hookimpl
    def build_tape_context(self) -> TapeContext:
        return TapeContext(select=_select_tape_context)

    @hookimpl
    def system_prompt(self, prompt: str | list[dict], state: TurnState) -> str:
        del prompt, state
        return ACP_PLAN_SYSTEM_PROMPT

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        @app.command("acp", help="Run Bub as an ACP agent.")
        def acp(command: str | None = typer.Argument(None, metavar="[serve]")) -> None:
            if command == "serve":
                typer.echo(
                    "Warning: `bub acp serve` is deprecated; use `bub acp` instead.",
                    err=True,
                )
            elif command is not None:
                raise typer.BadParameter(
                    f"Got unexpected extra argument {command!r}",
                    param_hint="command",
                )
            asyncio.run(run_acp_agent(self.framework))
