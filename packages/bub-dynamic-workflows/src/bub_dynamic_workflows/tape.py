from __future__ import annotations

from typing import Any, Protocol

from bub_dynamic_workflows.spec import WorkflowNode
from bub_dynamic_workflows.state import WorkflowRunState


class WorkflowTape(Protocol):
    async def task_started(self, state: WorkflowRunState) -> None: ...
    async def task_resumed(self, state: WorkflowRunState) -> None: ...
    async def task_finished(self, state: WorkflowRunState) -> None: ...
    async def task_failed(self, state: WorkflowRunState, error: str) -> None: ...
    async def task_cancelled(self, state: WorkflowRunState) -> None: ...
    async def node_started(self, state: WorkflowRunState, node: WorkflowNode, attempt: int) -> None: ...
    async def node_finished(self, state: WorkflowRunState, node: WorkflowNode, output: Any) -> None: ...
    async def node_failed(self, state: WorkflowRunState, node: WorkflowNode, error: str) -> None: ...
    async def node_skipped(self, state: WorkflowRunState, node: WorkflowNode, reason: str) -> None: ...
    async def checkpoint(self, state: WorkflowRunState) -> None: ...


class NullWorkflowTape:
    async def task_started(self, state: WorkflowRunState) -> None:
        pass

    async def task_resumed(self, state: WorkflowRunState) -> None:
        pass

    async def task_finished(self, state: WorkflowRunState) -> None:
        pass

    async def task_failed(self, state: WorkflowRunState, error: str) -> None:
        pass

    async def task_cancelled(self, state: WorkflowRunState) -> None:
        pass

    async def node_started(self, state: WorkflowRunState, node: WorkflowNode, attempt: int) -> None:
        pass

    async def node_finished(self, state: WorkflowRunState, node: WorkflowNode, output: Any) -> None:
        pass

    async def node_failed(self, state: WorkflowRunState, node: WorkflowNode, error: str) -> None:
        pass

    async def node_skipped(self, state: WorkflowRunState, node: WorkflowNode, reason: str) -> None:
        pass

    async def checkpoint(self, state: WorkflowRunState) -> None:
        pass


class BubWorkflowTape:
    def __init__(self, agent: Any, tape_name: str | None) -> None:
        if not tape_name:
            raise ValueError("workflow commands require an active tape")
        self.agent = agent
        self.tape_name = tape_name

    async def task_started(self, state: WorkflowRunState) -> None:
        await self._anchor("task_init", {"run_id": state.run_id, "spec": state.spec, "args": state.args})
        await self._event("workflow.run.started", {"run_id": state.run_id, "args": state.args})

    async def task_resumed(self, state: WorkflowRunState) -> None:
        await self._event("workflow.run.resumed", self._summary(state))

    async def task_finished(self, state: WorkflowRunState) -> None:
        await self._anchor("task_finish", self._summary(state))
        await self._event("workflow.run.completed", self._summary(state))

    async def task_failed(self, state: WorkflowRunState, error: str) -> None:
        await self._anchor("task_error", {**self._summary(state), "error": error})
        await self._event("workflow.run.failed", {**self._summary(state), "error": error})

    async def task_cancelled(self, state: WorkflowRunState) -> None:
        await self._anchor("task_cancelled", self._summary(state))
        await self._event("workflow.run.cancelled", self._summary(state))

    async def node_started(self, state: WorkflowRunState, node: WorkflowNode, attempt: int) -> None:
        data = {"run_id": state.run_id, "node_id": node.id, "attempt": attempt}
        await self._anchor(f"node/{node.id}/init", data)
        await self._event("workflow.node.started", data)

    async def node_finished(self, state: WorkflowRunState, node: WorkflowNode, output: Any) -> None:
        data = {"run_id": state.run_id, "node_id": node.id, "output": output}
        await self._anchor(f"node/{node.id}/finish", data)
        await self._event("workflow.node.completed", data)

    async def node_failed(self, state: WorkflowRunState, node: WorkflowNode, error: str) -> None:
        await self._event("workflow.node.failed", {"run_id": state.run_id, "node_id": node.id, "error": error})

    async def node_skipped(self, state: WorkflowRunState, node: WorkflowNode, reason: str) -> None:
        await self._event("workflow.node.skipped", {"run_id": state.run_id, "node_id": node.id, "reason": reason})

    async def checkpoint(self, state: WorkflowRunState) -> None:
        await self._anchor(f"dag_checkpoint/{state.checkpoint_seq}", self._summary(state))
        await self._event("workflow.checkpoint", self._summary(state))

    async def _anchor(self, suffix: str, payload: dict[str, Any]) -> None:
        await self.agent.tapes.handoff(self.tape_name, name=f"workflow/{payload['run_id']}/{suffix}", state=payload)

    async def _event(self, name: str, payload: dict[str, Any]) -> None:
        await self.agent.tapes.append_event(self.tape_name, name, payload)

    @staticmethod
    def _summary(state: WorkflowRunState) -> dict[str, Any]:
        return {
            "run_id": state.run_id,
            "status": state.status,
            "checkpoint_seq": state.checkpoint_seq,
            "nodes": {node_id: node.status for node_id, node in state.nodes.items()},
        }
