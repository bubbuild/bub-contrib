# bub-workflow

`bub-workflow` provides bee workflow tools for Bub. A workflow starts from a template, runs ready nodes, records evidence through Bub tape, and writes a task projection for status checks.

## Tools

- `workflow.start`: create a bee task from a template and run ready nodes when `execute` is true.
- `workflow.step`: run the currently ready nodes for an existing task.
- `workflow.status`: read the task projection for a run id.

## Templates

`workflow.start` uses one `template` object.

- Include `nodes` to define an inline template.
- Omit `nodes` to load a reusable template by `name`.
- Put run-specific values in `template.inputs`.

Reusable templates are resolved from the runtime `workflow_templates` registry first, then from configured template directories.

Template request fields:

- `name`: inline template name or reusable template name.
- `inputs`: values for this run.

Inline template definition fields:

- `description`: template scope.
- `skill`: optional task guidance.
- `config`: optional host metadata.
- `input_schema`: JSON Schema properties for `template.inputs`.
- `nodes`: DAG nodes.

Node fields:

- `id`: unique node id.
- `title`: readable milestone name.
- `description`: node scope.
- `executor`: `subagent` or `function`.
- `prompt`: node prompt. Required for `subagent`.
- `call`: Python callable target. Required for `function`.
- `depends_on`: node ids that must complete first.
- `output_schema`: optional JSON schema for node output.
- `allowed_tools`: optional tool allowlist for subagent execution.
- `allowed_skills`: optional skill allowlist for subagent execution.
- `features`: short notes about the node.

Prompt references:

- `{brief}`: task brief.
- `{inputs.name}`: template input value.
- `{nodes.node_id}`: full output from a completed node.
- `{nodes.node_id.field}`: field from a completed structured output.

## Configuration

Settings use Bub config and `pydantic-settings` with the `BUB_WORKFLOW_` environment prefix.

- `projection_dir` / `BUB_WORKFLOW_PROJECTION_DIR`: task projection directory. Default: `.bub/workflows`.
- `template_dirs` / `BUB_WORKFLOW_TEMPLATE_DIRS`: reusable template search directories. Default: `[".bub/workflow/templates"]`.

Relative paths are resolved from the current workspace. Absolute paths are used as provided.

## Tape Store

Workflow tape entries use the same tape store as Bub. During normal Bub CLI or channel execution, `WorkflowImpl` reads the store resolved by `BubFramework.running()` from the `provide_tape_store` hook. The hook may return a tape store directly, or a synchronous or asynchronous context manager that yields one.

If a tool context is assembled outside `WorkflowImpl.load_state`, `bub-workflow` falls back to the runtime Bub agent tape service when available. If no tape store is available, workflow execution still writes the task projection, but no workflow tape entries are recorded.

## Function Executor Example

This example runs a workflow without a model. The function executor calls the injected `_workflow_function_runner`.

```python
import asyncio
import json
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from republic import ToolContext

from bub_workflow.models import BeeStartInput
from bub_workflow.plugin import WORKFLOW_SCHEDULER_STATE_KEY
from bub_workflow.tools import workflow_start


async def workflow_function_runner(**kwargs: Any) -> dict[str, Any]:
    node = kwargs["node"]
    if node.id == "inventory":
        return {"modules": ["src/app.py"], "ok": True}
    return {
        "label": node.id,
        "ok": "src/app.py" in json.dumps(kwargs["outputs"]),
    }


async def main() -> None:
    scheduler = AsyncIOScheduler()
    scheduler.start()
    try:
        context = ToolContext(
            tape=None,
            run_id="workflow-demo",
            state={
                WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
                "_workflow_function_runner": workflow_function_runner,
            },
        )
        params = BeeStartInput.model_validate(
            {
                "run_id": "demo-bee",
                "brief": "Review repository maintainability.",
                "template": {
                    "name": "deterministic_review",
                    "description": "Review a repository with function nodes.",
                    "inputs": {"focus": "maintainability"},
                    "input_schema": {
                        "focus": {
                            "type": "string",
                            "default": "maintainability",
                        }
                    },
                    "nodes": [
                        {
                            "id": "inventory",
                            "title": "Inventory",
                            "executor": "function",
                            "call": "demo:inventory",
                            "prompt": "Inspect {inputs.focus} for {brief}",
                            "output_schema": {
                                "type": "object",
                                "properties": {
                                    "modules": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "ok": {"type": "boolean"},
                                },
                                "required": ["modules", "ok"],
                            },
                        },
                        {
                            "id": "verify",
                            "title": "Verify",
                            "executor": "function",
                            "call": "demo:verify",
                            "depends_on": ["inventory"],
                            "prompt": "Verify modules: {nodes.inventory.modules}",
                            "output_schema": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "ok": {"type": "boolean"},
                                },
                                "required": ["label", "ok"],
                            },
                        },
                    ],
                },
            }
        )
        result = await workflow_start.handler(
            **params.model_dump(mode="json"),
            context=context,
        )
        print(result)
    finally:
        scheduler.shutdown(wait=False)


asyncio.run(main())
```

## Reusable Template Example

Store a template bundle at `.bub/workflow/templates/repo_review/workflow.yaml`, then start a task with its name.

```yaml
name: repo_review
description: Review a repository through bee milestones.
input_schema:
  focus:
    type: string
    default: maintainability
nodes:
  - id: inventory
    title: Inventory
    executor: subagent
    prompt: Inspect this repository for {inputs.focus}. Return key modules and risks.
  - id: verify
    title: Verify
    executor: subagent
    depends_on:
      - inventory
    prompt: |-
      Verify these modules and risks:
      {nodes.inventory}
```

```python
params = BeeStartInput.model_validate(
    {
        "run_id": "demo-bee",
        "brief": "Review repository maintainability.",
        "template": {
            "name": "repo_review",
            "inputs": {"focus": "maintainability"},
        },
    }
)
```
