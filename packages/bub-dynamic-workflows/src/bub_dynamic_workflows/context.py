from __future__ import annotations

from pathlib import Path

from republic import ToolContext

from bub_dynamic_workflows.channel import WorkflowChannel
from bub_dynamic_workflows.controller import WorkflowController
from bub_dynamic_workflows.errors import WorkflowExecutionError
from bub_dynamic_workflows.runner import BubSubagentRunner
from bub_dynamic_workflows.state import WorkflowProjectionStore, WorkflowRunState
from bub_dynamic_workflows.tape import BubWorkflowTape


def controller_from_tool_context(context: ToolContext) -> WorkflowController:
    session_id = str(context.state.get("session_id", "workflow:default"))
    channel = context.state.get("workflow")
    if not isinstance(channel, WorkflowChannel):
        raise WorkflowExecutionError("workflow commands require a workflow channel in tool context")

    existing = channel.controller(session_id)
    if existing is not None:
        return existing

    agent = context.state.get("_runtime_agent")
    if agent is None:
        raise WorkflowExecutionError("workflow commands require _runtime_agent in tool context")
    workspace = context.state.get("_runtime_workspace")
    if not isinstance(workspace, str | Path):
        raise WorkflowExecutionError("workflow commands require _runtime_workspace in tool context")

    controller = WorkflowController(
        workspace=workspace,
        runner=BubSubagentRunner(context.state, context.tape),
        tape=BubWorkflowTape(agent, context.tape),
    )
    channel.bind_controller(session_id, controller)
    return controller


def status_from_tool_context(context: ToolContext, run_id: str) -> WorkflowRunState:
    session_id = str(context.state.get("session_id", "workflow:default"))
    channel = context.state.get("workflow")
    if isinstance(channel, WorkflowChannel):
        controller = channel.controller(session_id)
        if controller is not None:
            return controller.status(run_id)

    workspace = context.state.get("_runtime_workspace")
    if not isinstance(workspace, str | Path):
        raise WorkflowExecutionError("workflow.status requires _runtime_workspace in tool context")
    return WorkflowProjectionStore(workspace).read(run_id)
