from __future__ import annotations

from typing import Any, cast

from apscheduler.schedulers.base import BaseScheduler
from bub import tool
from republic import ToolContext

from bub_workflow.config import WorkflowSettings
from bub_workflow.constants import WORKFLOW_SCHEDULER_STATE_KEY, WORKFLOW_SETTINGS_STATE_KEY
from bub_workflow.models import BeeStartInput
from bub_workflow.runtime import BeeRuntime


@tool(name="workflow.start", context=True, model=BeeStartInput)
async def workflow_start(params: BeeStartInput, context: ToolContext) -> str:
    """Create a bee task from an inline or reusable template."""
    projection = await _runtime(context).start(params)
    return projection.model_dump_json(indent=2)


@tool(name="workflow.step", context=True)
async def workflow_step(run_id: str, context: ToolContext) -> str:
    """Execute all currently ready nodes for an existing bee task."""
    projection = await _runtime(context).step(run_id)
    return projection.model_dump_json(indent=2)


@tool(name="workflow.status", context=True)
def workflow_status(run_id: str, context: ToolContext) -> str:
    """Read one bee task projection from the current workspace."""
    projection = _runtime(context).status(run_id)
    return projection.model_dump_json(indent=2)


def _runtime(context: ToolContext) -> BeeRuntime:
    return BeeRuntime(
        scheduler=_ensure_scheduler(context.state),
        context=context,
        workspace=_workspace(context),
        settings=_settings(context.state),
    )


def _ensure_scheduler(state: dict[str, Any]) -> BaseScheduler:
    scheduler = state.get(WORKFLOW_SCHEDULER_STATE_KEY)
    if scheduler is None:
        raise RuntimeError("workflow scheduler not found in state, is WorkflowImpl plugin loaded?")
    return cast(BaseScheduler, scheduler)


def _workspace(context: ToolContext) -> str:
    workspace = context.state.get("_runtime_workspace")
    return str(workspace) if workspace else "."


def _settings(state: dict[str, Any]) -> WorkflowSettings:
    value = state.get(WORKFLOW_SETTINGS_STATE_KEY)
    if isinstance(value, WorkflowSettings):
        return value
    return WorkflowSettings()
