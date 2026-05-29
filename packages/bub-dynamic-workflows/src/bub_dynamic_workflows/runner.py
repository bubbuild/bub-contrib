from __future__ import annotations

from typing import Any, Mapping, Protocol

from republic import ToolContext

from bub.builtin.tools import run_subagent
from bub_dynamic_workflows.spec import WorkflowNode


class WorkflowRunner(Protocol):
    async def run_node(
        self,
        *,
        prompt: str,
        node: WorkflowNode,
        run_id: str,
        attempt: int,
        item_index: int | None = None,
    ) -> str: ...


class BubSubagentRunner:
    def __init__(self, state: Mapping[str, Any], tape_name: str | None) -> None:
        self.state = dict(state)
        self.tape_name = tape_name

    async def run_node(
        self,
        *,
        prompt: str,
        node: WorkflowNode,
        run_id: str,
        attempt: int,
        item_index: int | None = None,
    ) -> str:
        session_parts = [f"temp/workflow-{run_id}-{node.id}-{attempt}"]
        if item_index is not None:
            session_parts.append(str(item_index))
        session_id = "-".join(session_parts)
        context = ToolContext(
            tape=self.tape_name,
            run_id=f"workflow:{run_id}:{node.id}:{attempt}",
            state=self.state,
        )
        return await run_subagent.run(
            session=session_id,
            prompt=prompt,
            model=node.model,
            allowed_tools=node.allowed_tools,
            allowed_skills=node.allowed_skills,
            context=context,
        )
