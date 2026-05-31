from __future__ import annotations

from typing import Any

from republic import ToolContext
from republic.tape.manager import AsyncTapeManager
from republic.tape.store import TapeEntry

from bub_workflow.constants import WORKFLOW_TAPE_STORE_STATE_KEY
from bub_workflow.models import BeeNodeProjection, BeeProjection


class WorkflowTape:
    def __init__(self, context: ToolContext) -> None:
        self.tape_name = context.tape
        store = _store_from_state(context.state)
        self.manager = _manager_for(store)

    async def task_started(self, projection: BeeProjection) -> None:
        await self._anchor("bee_task_init", projection.run_id, self._summary(projection))
        await self._event("bee.task.started", self._summary(projection))

    async def agent_started(self, projection: BeeProjection, node: BeeNodeProjection) -> None:
        await self._anchor(
            f"bee_node/{node.id}/init",
            projection.run_id,
            self._node_summary(projection, node),
        )
        await self._event("bee.node.started", self._node_summary(projection, node))

    async def agent_finished(self, projection: BeeProjection, node: BeeNodeProjection) -> None:
        await self._anchor(
            f"bee_node/{node.id}/finish",
            projection.run_id,
            self._node_summary(projection, node),
        )
        await self._event("bee.node.completed", self._node_summary(projection, node))

    async def checkpoint(self, projection: BeeProjection) -> None:
        await self._anchor("bee_dag_checkpoint", projection.run_id, self._summary(projection))
        await self._event("bee.dag.checkpoint", self._summary(projection))

    async def task_finished(self, projection: BeeProjection) -> None:
        await self._anchor("bee_task_fin", projection.run_id, self._summary(projection))
        await self._event("bee.task.completed", self._summary(projection))

    async def task_failed(self, projection: BeeProjection) -> None:
        await self._anchor("bee_task_error", projection.run_id, self._summary(projection))
        await self._event("bee.task.failed", self._summary(projection))

    async def _anchor(self, suffix: str, run_id: str, payload: dict[str, Any]) -> None:
        if self.manager is None or not self.tape_name:
            return
        await self.manager.handoff(
            self.tape_name,
            f"bee/{run_id}/{suffix}",
            state=payload,
            run_id=run_id,
        )

    async def _event(self, name: str, payload: dict[str, Any]) -> None:
        if self.manager is None or not self.tape_name:
            return
        await self.manager.append_entry(
            self.tape_name,
            TapeEntry.event(name, payload, run_id=payload.get("run_id")),
        )

    @staticmethod
    def _summary(projection: BeeProjection) -> dict[str, Any]:
        return {
            "run_id": projection.run_id,
            "template_name": projection.template_name,
            "status": projection.status,
            "nodes": {node_id: node.status for node_id, node in projection.nodes.items()},
            "error": projection.error,
        }

    @staticmethod
    def _node_summary(projection: BeeProjection, node: BeeNodeProjection) -> dict[str, Any]:
        return {
            "run_id": projection.run_id,
            "template_name": projection.template_name,
            "node_id": node.id,
            "title": node.title,
            "status": node.status,
            "error": node.error,
        }


def _manager_for(store: Any) -> AsyncTapeManager | None:
    if store is None or not _is_tape_store(store):
        return None
    return AsyncTapeManager(store=store)


def _store_from_state(state: dict[str, Any]) -> Any:
    if WORKFLOW_TAPE_STORE_STATE_KEY in state:
        return state[WORKFLOW_TAPE_STORE_STATE_KEY]
    agent = state.get("_runtime_agent")
    tapes = getattr(agent, "tapes", None)
    return getattr(tapes, "_store", None)


def _is_tape_store(value: Any) -> bool:
    return all(hasattr(value, name) for name in ("list_tapes", "reset", "fetch_all", "append"))
