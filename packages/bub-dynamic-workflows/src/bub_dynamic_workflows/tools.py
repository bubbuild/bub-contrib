from __future__ import annotations

from typing import Any

from bub import tool
from pydantic import BaseModel, Field
from republic import ToolContext

from bub_dynamic_workflows.context import controller_from_tool_context, status_from_tool_context
from bub_dynamic_workflows.controller import spec_from_inputs
from bub_dynamic_workflows.errors import WorkflowExecutionError
from bub_dynamic_workflows.spec import load_mapping_file


class WorkflowStartInput(BaseModel):
    spec_path: str | None = Field(None, description="Path to a workflow spec or bee template directory.")
    spec: dict[str, Any] | None = Field(None, description="Inline workflow specification.")
    args: dict[str, Any] = Field(default_factory=dict, description="Workflow input args.")
    args_path: str | None = Field(None, description="Path to workflow args JSON/YAML/TOML.")
    run_id: str | None = Field(None, description="Optional run id.")
    background: bool = Field(False, description="Start in the live Bub process and return immediately.")


class WorkflowRunIdInput(BaseModel):
    run_id: str = Field(..., description="Workflow run id.")


@tool(context=True, name="workflow.start", model=WorkflowStartInput)
async def workflow_start(param: WorkflowStartInput, *, context: ToolContext) -> str:
    spec = spec_from_inputs(spec=param.spec, spec_path=param.spec_path)
    state = await controller_from_tool_context(context).start(
        spec=spec,
        args=_args_from_input(param.args, param.args_path),
        run_id=param.run_id,
        background=param.background,
    )
    return state.model_dump_json(indent=2)


@tool(context=True, name="workflow.resume", model=WorkflowRunIdInput)
async def workflow_resume(param: WorkflowRunIdInput, *, context: ToolContext) -> str:
    state = await controller_from_tool_context(context).resume(run_id=param.run_id)
    return state.model_dump_json(indent=2)


@tool(context=True, name="workflow.status", model=WorkflowRunIdInput)
def workflow_status(param: WorkflowRunIdInput, *, context: ToolContext) -> str:
    state = status_from_tool_context(context, param.run_id)
    return state.model_dump_json(indent=2)


@tool(context=True, name="workflow.cancel", model=WorkflowRunIdInput)
async def workflow_cancel(param: WorkflowRunIdInput, *, context: ToolContext) -> str:
    state = await controller_from_tool_context(context).cancel(param.run_id)
    return state.model_dump_json(indent=2)


def _args_from_input(args: dict[str, Any], args_path: str | None) -> dict[str, Any]:
    if args and args_path:
        raise WorkflowExecutionError("provide either args or args_path, not both")
    if args_path is None:
        return args
    return load_mapping_file(args_path)
