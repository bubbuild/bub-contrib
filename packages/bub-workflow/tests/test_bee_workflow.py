from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from republic import ToolContext
from republic.tape.store import InMemoryTapeStore

from bub_workflow.config import WorkflowSettings
from bub_workflow.models import BeeStartInput
from bub_workflow.plugin import (
    WORKFLOW_SCHEDULER_STATE_KEY,
    WORKFLOW_SETTINGS_STATE_KEY,
    WORKFLOW_TAPE_STORE_STATE_KEY,
)
from bub_workflow.tools import workflow_start, workflow_status, workflow_step


@pytest.fixture
async def scheduler():
    scheduler = AsyncIOScheduler()
    scheduler.start()
    try:
        yield scheduler
    finally:
        scheduler.shutdown(wait=False)


async def test_workflow_start_runs_temporary_template_with_function_nodes(
    tmp_path,
    scheduler,
) -> None:
    calls: list[dict[str, Any]] = []

    async def runner(**kwargs: Any) -> dict[str, Any]:
        node = kwargs["node"]
        calls.append({"node_id": node.id, "prompt": kwargs["prompt"]})
        if node.id == "inventory":
            return {"modules": ["src/app.py"], "ok": True}
        return {"label": node.id, "ok": "src/app.py" in json.dumps(kwargs["outputs"])}

    context = ToolContext(
        tape=None,
        run_id="tool-run",
        state={
            WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
            "_runtime_workspace": str(tmp_path),
            "_workflow_function_runner": runner,
        },
    )
    params = BeeStartInput.model_validate(
        {
            "run_id": "bee-1",
            "brief": "Review repository maintainability.",
            "template": _template(inputs={"focus": "maintainability"}),
        }
    )

    output = await workflow_start.handler(**params.model_dump(mode="json"), context=context)
    projection = json.loads(output)

    assert projection["run_id"] == "bee-1"
    assert projection["status"] == "completed"
    assert projection["result"]["inventory"] == {"modules": ["src/app.py"], "ok": True}
    assert projection["result"]["verify"] == {"label": "verify", "ok": True}
    assert calls == [
        {
            "node_id": "inventory",
            "prompt": "Inspect maintainability for Review repository maintainability.",
        },
        {"node_id": "verify", "prompt": "Verify modules: [\n  \"src/app.py\"\n]"},
    ]

    status = json.loads(await workflow_status.handler("bee-1", context=context))
    assert status["nodes"]["inventory"]["status"] == "completed"
    assert (tmp_path / ".bub" / "workflows" / "bee-1" / "task.json").is_file()


async def test_workflow_start_runs_reusable_template_from_workspace(tmp_path, scheduler) -> None:
    async def runner(**kwargs: Any) -> dict[str, Any]:
        node = kwargs["node"]
        if node.id == "inventory":
            return {"modules": ["src/app.py"], "ok": True}
        return {"label": node.id, "ok": "src/app.py" in json.dumps(kwargs["outputs"])}

    template_dir = tmp_path / ".bub" / "workflow" / "templates"
    _write_template_bundle(template_dir, "repo_review", _template())

    context = ToolContext(
        tape=None,
        run_id="tool-run",
        state={
            WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
            "_runtime_workspace": str(tmp_path),
            "_workflow_function_runner": runner,
        },
    )
    params = BeeStartInput.model_validate(
        {
            "run_id": "bee-template-ref",
            "brief": "Review repository maintainability.",
            "template": {
                "name": "repo_review",
                "inputs": {"focus": "maintainability"},
            },
        }
    )

    projection = json.loads(
        await workflow_start.handler(**params.model_dump(mode="json"), context=context)
    )

    assert projection["template_source"] == "repo_review"
    assert projection["template_name"] == "repo_review_template"
    assert projection["inputs"] == {"focus": "maintainability"}
    assert projection["status"] == "completed"
    assert projection["result"]["verify"] == {"label": "verify", "ok": True}


async def test_workflow_start_uses_configured_template_and_projection_dirs(
    tmp_path,
    scheduler,
) -> None:
    async def runner(**kwargs: Any) -> dict[str, Any]:
        node = kwargs["node"]
        if node.id == "inventory":
            return {"modules": ["src/app.py"], "ok": True}
        return {"label": node.id, "ok": "src/app.py" in json.dumps(kwargs["outputs"])}

    settings = WorkflowSettings(
        projection_dir="state/workflows",
        template_dirs=["config/templates"],
    )
    template_dir = tmp_path / "config" / "templates"
    _write_template_bundle(template_dir, "repo_review", _template())

    context = ToolContext(
        tape=None,
        run_id="tool-run",
        state={
            WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
            WORKFLOW_SETTINGS_STATE_KEY: settings,
            "_runtime_workspace": str(tmp_path),
            "_workflow_function_runner": runner,
        },
    )
    params = BeeStartInput.model_validate(
        {
            "run_id": "configured-dirs",
            "brief": "Review repository maintainability.",
            "template": {
                "name": "repo_review",
                "inputs": {"focus": "maintainability"},
            },
        }
    )

    projection = json.loads(
        await workflow_start.handler(**params.model_dump(mode="json"), context=context)
    )

    assert projection["status"] == "completed"
    assert (tmp_path / "state" / "workflows" / "configured-dirs" / "task.json").is_file()


async def test_workflow_start_applies_template_input_defaults(tmp_path, scheduler) -> None:
    calls: list[dict[str, Any]] = []

    async def runner(**kwargs: Any) -> dict[str, Any]:
        calls.append({"prompt": kwargs["prompt"], "inputs": kwargs["inputs"]})
        return {"modules": ["src/app.py"], "ok": True}

    context = ToolContext(
        tape=None,
        run_id="tool-run",
        state={
            WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
            "_runtime_workspace": str(tmp_path),
            "_workflow_function_runner": runner,
        },
    )
    params = BeeStartInput.model_validate(
        {
            "run_id": "defaults-1",
            "brief": "Review repository maintainability.",
            "template": {
                "name": "defaults_template",
                "input_schema": {
                    "focus": {
                        "type": "string",
                        "default": "maintainability",
                    }
                },
                "nodes": [
                    {
                        "id": "inventory",
                        "executor": "function",
                        "call": "tests.fake:inventory",
                        "prompt": "Inspect {inputs.focus} for {brief}",
                        "output_schema": _inventory_schema(),
                    }
                ],
            },
        }
    )

    projection = json.loads(
        await workflow_start.handler(**params.model_dump(mode="json"), context=context)
    )

    assert projection["inputs"] == {"focus": "maintainability"}
    assert calls == [
        {
            "prompt": "Inspect maintainability for Review repository maintainability.",
            "inputs": {"focus": "maintainability"},
        }
    ]


async def test_workflow_start_validates_template_inputs(tmp_path, scheduler) -> None:
    context = ToolContext(
        tape=None,
        run_id="tool-run",
        state={
            WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
            "_runtime_workspace": str(tmp_path),
            "_workflow_function_runner": lambda **kwargs: {"ok": True},
        },
    )
    params = BeeStartInput.model_validate(
        {
            "run_id": "bad-inputs-1",
            "brief": "Review repository maintainability.",
            "template": {
                "name": "bad_inputs_template",
                "inputs": {"focus": 123},
                "input_schema": {"focus": {"type": "string"}},
                "nodes": [
                    {
                        "id": "inventory",
                        "executor": "function",
                        "call": "tests.fake:inventory",
                    }
                ],
            },
        }
    )

    with pytest.raises(RuntimeError, match="inputs failed validation"):
        await workflow_start.handler(**params.model_dump(mode="json"), context=context)


def test_reusable_template_request_rejects_inline_definition_fields() -> None:
    with pytest.raises(ValueError, match="reusable template request cannot define"):
        BeeStartInput.model_validate(
            {
                "run_id": "bad-template-request",
                "brief": "Review repository maintainability.",
                "template": {
                    "name": "repo_review",
                    "input_schema": {
                        "focus": {
                            "type": "string",
                        }
                    },
                },
            }
        )


async def test_workflow_start_records_bee_anchors_through_tape_store(tmp_path, scheduler) -> None:
    store = InMemoryTapeStore()

    async def runner(**kwargs: Any) -> dict[str, Any]:
        return {"modules": ["src/app.py"], "ok": True}

    context = ToolContext(
        tape="bee-tape",
        run_id="tool-run",
        state={
            WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
            WORKFLOW_TAPE_STORE_STATE_KEY: store,
            "_runtime_workspace": str(tmp_path),
            "_workflow_function_runner": runner,
        },
    )
    params = BeeStartInput.model_validate(
        {
            "run_id": "bee-tape-run",
            "brief": "Collect inventory.",
            "template": {
                "name": "inventory_template",
                "nodes": [
                    {
                        "id": "inventory",
                        "executor": "function",
                        "call": "tests.fake:inventory",
                        "output_schema": _inventory_schema(),
                    }
                ],
            },
        }
    )

    await workflow_start.handler(**params.model_dump(mode="json"), context=context)

    entries = store.read("bee-tape")
    assert entries is not None
    anchors = [entry.payload["name"] for entry in entries if entry.kind == "anchor"]
    events = [entry.payload["name"] for entry in entries if entry.kind == "event"]
    assert "bee/bee-tape-run/bee_task_init" in anchors
    assert "bee/bee-tape-run/bee_node/inventory/init" in anchors
    assert "bee/bee-tape-run/bee_node/inventory/finish" in anchors
    assert "bee/bee-tape-run/bee_dag_checkpoint" in anchors
    assert "bee/bee-tape-run/bee_task_fin" in anchors
    assert "bee.task.started" in events
    assert "bee.node.completed" in events


async def test_workflow_start_uses_runtime_agent_tape_store(tmp_path, scheduler) -> None:
    store = InMemoryTapeStore()

    async def runner(**kwargs: Any) -> dict[str, Any]:
        return {"modules": ["src/app.py"], "ok": True}

    context = ToolContext(
        tape="agent-tape",
        run_id="tool-run",
        state={
            WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
            "_runtime_agent": SimpleNamespace(tapes=SimpleNamespace(_store=store)),
            "_runtime_workspace": str(tmp_path),
            "_workflow_function_runner": runner,
        },
    )
    params = BeeStartInput.model_validate(
        {
            "run_id": "agent-tape-run",
            "brief": "Collect inventory.",
            "template": {
                "name": "inventory_template",
                "nodes": [
                    {
                        "id": "inventory",
                        "executor": "function",
                        "call": "tests.fake:inventory",
                        "output_schema": _inventory_schema(),
                    }
                ],
            },
        }
    )

    await workflow_start.handler(**params.model_dump(mode="json"), context=context)

    entries = store.read("agent-tape")
    assert entries is not None
    anchors = [entry.payload["name"] for entry in entries if entry.kind == "anchor"]
    assert "bee/agent-tape-run/bee_task_init" in anchors
    assert "bee/agent-tape-run/bee_task_fin" in anchors


async def test_workflow_start_records_schema_error(tmp_path, scheduler) -> None:
    async def runner(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"ok": True}

    context = ToolContext(
        tape=None,
        run_id="tool-run",
        state={
            WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
            "_runtime_workspace": str(tmp_path),
            "_workflow_function_runner": runner,
        },
    )
    params = BeeStartInput.model_validate(
        {
            "run_id": "broken-1",
            "brief": "Broken run.",
            "template": {
                "name": "broken_template",
                "nodes": [
                    {
                        "id": "inventory",
                        "executor": "function",
                        "call": "tests.fake:inventory",
                        "output_schema": _inventory_schema(),
                    }
                ],
            },
        }
    )

    output = json.loads(
        await workflow_start.handler(**params.model_dump(mode="json"), context=context)
    )
    assert output["status"] == "failed"

    status = json.loads(await workflow_status.handler("broken-1", context=context))
    assert status["status"] == "failed"
    assert status["nodes"]["inventory"]["status"] == "failed"
    error = status["nodes"]["inventory"]["error"]
    assert "required property" in error or "is a required property" in error


async def test_workflow_step_resumes_template_metadata_from_projection(tmp_path, scheduler) -> None:
    async def runner(**kwargs: Any) -> dict[str, Any]:
        node = kwargs["node"]
        if node.id == "inventory":
            return {"modules": ["src/app.py"], "ok": True}
        return {"label": "verify", "ok": "src/app.py" in json.dumps(kwargs["outputs"])}

    context = ToolContext(
        tape=None,
        run_id="tool-run",
        state={
            WORKFLOW_SCHEDULER_STATE_KEY: scheduler,
            "_runtime_workspace": str(tmp_path),
            "_workflow_function_runner": runner,
        },
    )
    params = BeeStartInput.model_validate(
        {
            "run_id": "stepped-1",
            "brief": "Review repository maintainability.",
            "template": _template(inputs={"focus": "maintainability"}),
            "execute": False,
        }
    )

    created = json.loads(
        await workflow_start.handler(**params.model_dump(mode="json"), context=context)
    )
    assert created["status"] == "pending"

    stepped = json.loads(await workflow_step.handler("stepped-1", context=context))

    assert stepped["status"] == "completed"
    assert stepped["nodes"]["inventory"]["output_schema"] == _inventory_schema()
    assert stepped["nodes"]["verify"]["output"] == {"label": "verify", "ok": True}


def _template(inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    template = {
        "name": "repo_review_template",
        "description": "Review a repository with bee milestones.",
        "skill": "Use this bee to inspect and verify repository maintainability.",
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
                "call": "tests.fake:inventory",
                "prompt": "Inspect {inputs.focus} for {brief}",
                "output_schema": _inventory_schema(),
                "features": ["inventory lists modules"],
            },
            {
                "id": "verify",
                "title": "Verify",
                "executor": "function",
                "call": "tests.fake:verify",
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
                "features": ["verification uses inventory"],
            },
        ],
    }
    if inputs is not None:
        template["inputs"] = inputs
    return template


def _write_template_bundle(
    root: Path,
    name: str,
    template: dict[str, Any],
) -> None:
    bundle = root / name
    bundle.mkdir(parents=True)
    (bundle / "workflow.yaml").write_text(
        yaml.safe_dump(template, sort_keys=False),
        encoding="utf-8",
    )


def _inventory_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "modules": {"type": "array", "items": {"type": "string"}},
            "ok": {"type": "boolean"},
        },
        "required": ["modules", "ok"],
    }
