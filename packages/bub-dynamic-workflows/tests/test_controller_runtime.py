from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import pytest

from bub_dynamic_workflows.controller import WorkflowController
from bub_dynamic_workflows.runner import WorkflowRunner
from bub_dynamic_workflows.spec import WorkflowNode, load_workflow_spec
from bub_dynamic_workflows.state import RunStatus, WorkflowRunState
from bub_dynamic_workflows.tape import WorkflowTape


class FakeRunner(WorkflowRunner):
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, str]] = []

    async def run_node(
        self,
        *,
        prompt: str,
        node: WorkflowNode,
        run_id: str,
        attempt: int,
        item_index: int | None = None,
    ) -> str:
        del run_id, attempt, item_index
        self.calls.append((node.id, prompt))
        output = self.outputs[node.id]
        if output == "__raise__":
            raise RuntimeError(f"{node.id} failed")
        if output == "__sleep__":
            await asyncio.sleep(60)
        return output


class ConcurrentRunner(WorkflowRunner):
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

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
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return "ok"


class FakeTape(WorkflowTape):
    def __init__(self) -> None:
        self.records: list[tuple[str, str, Any]] = []

    async def task_started(self, state: WorkflowRunState) -> None:
        self.records.append(("task", "started", state.run_id))

    async def task_resumed(self, state: WorkflowRunState) -> None:
        self.records.append(("task", "resumed", state.run_id))

    async def task_finished(self, state: WorkflowRunState) -> None:
        self.records.append(("task", "finished", state.run_id))

    async def task_failed(self, state: WorkflowRunState, error: str) -> None:
        self.records.append(("task", "failed", error))

    async def task_cancelled(self, state: WorkflowRunState) -> None:
        self.records.append(("task", "cancelled", state.run_id))

    async def node_started(self, state: WorkflowRunState, node: WorkflowNode, attempt: int) -> None:
        self.records.append(("node", "started", (node.id, attempt)))

    async def node_finished(self, state: WorkflowRunState, node: WorkflowNode, output: Any) -> None:
        self.records.append(("node", "finished", (node.id, output)))

    async def node_failed(self, state: WorkflowRunState, node: WorkflowNode, error: str) -> None:
        self.records.append(("node", "failed", (node.id, error)))

    async def node_skipped(self, state: WorkflowRunState, node: WorkflowNode, reason: str) -> None:
        self.records.append(("node", "skipped", (node.id, reason)))

    async def checkpoint(self, state: WorkflowRunState) -> None:
        self.records.append(("task", "checkpoint", state.checkpoint_seq))


@pytest.mark.asyncio
async def test_controller_starts_workflow_and_records_tape(tmp_path) -> None:
    spec = load_workflow_spec(
        {
            "name": "demo",
            "description": "Demo workflow",
            "nodes": [
                {"id": "scan", "prompt": "scan {args.repo}"},
                {"id": "summary", "depends_on": ["scan"], "prompt": "summary {nodes.scan}"},
            ],
        }
    )
    runner = FakeRunner({"scan": "inventory", "summary": "done"})
    tape = FakeTape()
    controller = WorkflowController(workspace=tmp_path, runner=runner, tape=tape)

    state = await controller.start(spec=spec, args={"repo": "."}, run_id="run-1")

    assert state.status == RunStatus.COMPLETED
    assert state.nodes["summary"].output == "done"
    assert controller.status("run-1").status == RunStatus.COMPLETED
    assert ("task", "finished", "run-1") in tape.records
    assert runner.calls[1][1].endswith("summary inventory")


@pytest.mark.asyncio
async def test_controller_skips_dependents_after_failure(tmp_path) -> None:
    spec = load_workflow_spec(
        {
            "name": "demo",
            "description": "Demo workflow",
            "nodes": [
                {"id": "scan", "prompt": "scan"},
                {"id": "summary", "depends_on": ["scan"], "prompt": "summary"},
            ],
        }
    )
    controller = WorkflowController(workspace=tmp_path, runner=FakeRunner({"scan": "__raise__"}), tape=FakeTape())

    state = await controller.start(spec=spec, run_id="run-1")

    assert state.status == RunStatus.FAILED
    assert state.nodes["scan"].status == "failed"
    assert state.nodes["summary"].status == "skipped"


@pytest.mark.asyncio
async def test_resume_reuses_completed_node_outputs(tmp_path) -> None:
    spec = load_workflow_spec(
        {
            "name": "demo",
            "description": "Demo workflow",
            "nodes": [
                {"id": "scan", "prompt": "scan"},
                {"id": "summary", "depends_on": ["scan"], "prompt": "summary {nodes.scan}"},
            ],
        }
    )
    runner = FakeRunner({"scan": "inventory", "summary": "__raise__"})
    controller = WorkflowController(workspace=tmp_path, runner=runner, tape=FakeTape())

    failed = await controller.start(spec=spec, run_id="run-1")
    runner.outputs["summary"] = "done"
    runner.calls.clear()
    resumed = await controller.resume(run_id="run-1")

    assert failed.status == RunStatus.FAILED
    assert resumed.status == RunStatus.COMPLETED
    assert [node_id for node_id, _ in runner.calls] == ["summary"]
    assert "summary inventory" in runner.calls[0][1]


@pytest.mark.asyncio
async def test_controller_cancels_background_run(tmp_path) -> None:
    spec = load_workflow_spec(
        {
            "name": "demo",
            "description": "Demo workflow",
            "nodes": [{"id": "slow", "prompt": "slow"}],
        }
    )
    controller = WorkflowController(workspace=tmp_path, runner=FakeRunner({"slow": "__sleep__"}), tape=FakeTape())
    await controller.start(spec=spec, run_id="run-1", background=True)

    state = await controller.cancel("run-1")

    assert state.status == RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_runtime_validates_json_output_schema(tmp_path) -> None:
    spec = load_workflow_spec(
        {
            "name": "demo",
            "description": "Demo workflow",
            "nodes": [
                {
                    "id": "scan",
                    "prompt": "scan",
                    "output_schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                    },
                }
            ],
        }
    )
    controller = WorkflowController(workspace=tmp_path, runner=FakeRunner({"scan": json.dumps({"ok": True})}), tape=FakeTape())

    state = await controller.start(spec=spec, run_id="run-1")

    assert state.nodes["scan"].output == {"ok": True}


@pytest.mark.asyncio
async def test_foreach_runs_one_prompt_per_item(tmp_path) -> None:
    spec = load_workflow_spec(
        {
            "name": "demo",
            "description": "Demo workflow",
            "nodes": [{"id": "review", "foreach": "args.items", "prompt": "review {item}"}],
        }
    )
    runner = FakeRunner({"review": "ok"})
    controller = WorkflowController(workspace=tmp_path, runner=runner, tape=FakeTape())

    state = await controller.start(spec=spec, args={"items": ["a", "b"]}, run_id="run-1")

    assert state.nodes["review"].output == ["ok", "ok"]
    assert [attempt.item_index for attempt in state.nodes["review"].attempts] == [0, 1]
    assert [prompt for _, prompt in runner.calls] == [
        "Workflow: demo\n\nDescription: Demo workflow\n\nNode: review\n\nTask prompt:\nreview a",
        "Workflow: demo\n\nDescription: Demo workflow\n\nNode: review\n\nTask prompt:\nreview b",
    ]

    redun_db = tmp_path / ".bub" / "workflows" / "run-1" / "redun.db"
    with sqlite3.connect(redun_db) as conn:
        task_names = {row[0] for row in conn.execute("select name from task")}
        item_jobs = conn.execute(
            """
            select count(*)
            from job
            join task on job.task_hash = task.hash
            where task.name = 'workflow_foreach_item'
            """
        ).fetchone()[0]

    assert "workflow_foreach_item" in task_names
    assert item_jobs == 2


@pytest.mark.asyncio
async def test_foreach_respects_node_concurrency(tmp_path) -> None:
    spec = load_workflow_spec(
        {
            "name": "demo",
            "description": "Demo workflow",
            "concurrency": 4,
            "nodes": [
                {
                    "id": "review",
                    "foreach": "args.items",
                    "concurrency": 1,
                    "prompt": "review {item}",
                }
            ],
        }
    )
    runner = ConcurrentRunner()
    controller = WorkflowController(workspace=tmp_path, runner=runner, tape=FakeTape())

    state = await controller.start(spec=spec, args={"items": ["a", "b", "c"]}, run_id="run-1")

    assert state.status == RunStatus.COMPLETED
    assert runner.max_active == 1
