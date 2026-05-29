from __future__ import annotations

import pytest
from republic import ToolContext

from bub.tools import REGISTRY
from bub_dynamic_workflows.channel import WorkflowChannel
from bub_dynamic_workflows.controller import WorkflowController
from bub_dynamic_workflows.errors import WorkflowExecutionError
from bub_dynamic_workflows.runner import WorkflowRunner
from bub_dynamic_workflows.spec import WorkflowNode
from bub_dynamic_workflows.tape import NullWorkflowTape
from bub_dynamic_workflows.tools import workflow_status


def test_validate_tool_is_not_registered() -> None:
    assert "workflow.validate" not in REGISTRY


async def test_status_tool_uses_state_workflow_channel(tmp_path) -> None:
    controller = WorkflowController(workspace=tmp_path, runner=NoopRunner(), tape=NullWorkflowTape())
    channel = WorkflowChannel(framework=object())
    channel.bind_controller("workflow:test", controller)
    controller.store.write(_state_for_status())

    result = await workflow_status.run(
        run_id="status-1",
        context=ToolContext(tape="tape", run_id="run", state={"session_id": "workflow:test", "workflow": channel}),
    )

    assert '"run_id": "status-1"' in result


async def test_status_tool_reads_workspace_without_active_tape(tmp_path) -> None:
    controller = WorkflowController(workspace=tmp_path, runner=NoopRunner(), tape=NullWorkflowTape())
    controller.store.write(_state_for_status())

    result = await workflow_status.run(
        run_id="status-1",
        context=ToolContext(tape=None, run_id="run", state={"_runtime_workspace": str(tmp_path)}),
    )

    assert '"run_id": "status-1"' in result


async def test_status_tool_requires_workspace_without_channel() -> None:
    with pytest.raises(WorkflowExecutionError, match="_runtime_workspace"):
        await workflow_status.run(
            run_id="status-1",
            context=ToolContext(tape=None, run_id="run", state={}),
        )


class NoopRunner(WorkflowRunner):
    async def run_node(
        self,
        *,
        prompt: str,
        node: WorkflowNode,
        run_id: str,
        attempt: int,
        item_index: int | None = None,
    ) -> str:
        del prompt, node, run_id, attempt, item_index
        return ""


def _state_for_status():
    from bub_dynamic_workflows.spec import load_workflow_spec
    from bub_dynamic_workflows.state import WorkflowRunState

    spec = load_workflow_spec(
        {
            "name": "status_demo",
            "description": "Status demo",
            "nodes": [{"id": "node", "prompt": "run"}],
        }
    )
    return WorkflowRunState.create(run_id="status-1", spec=spec, args={})
