from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema

from bub_dynamic_workflows.errors import WorkflowExecutionError, WorkflowStateError
from bub_dynamic_workflows.runner import WorkflowRunner
from bub_dynamic_workflows.runtime import WorkflowRuntime
from bub_dynamic_workflows.spec import WorkflowSpec, load_workflow_spec, load_workflow_spec_file
from bub_dynamic_workflows.state import RunStatus, WorkflowProjectionStore, WorkflowRunState
from bub_dynamic_workflows.tape import WorkflowTape


class WorkflowController:
    def __init__(
        self,
        *,
        workspace: str | Path,
        runner: WorkflowRunner,
        tape: WorkflowTape,
    ) -> None:
        self.store = WorkflowProjectionStore(workspace)
        self.runner = runner
        self.tape = tape
        self._tasks: dict[str, asyncio.Task[WorkflowRunState]] = {}

    async def start(
        self,
        *,
        spec: WorkflowSpec,
        args: dict[str, Any] | None = None,
        run_id: str | None = None,
        background: bool = False,
    ) -> WorkflowRunState:
        args = dict(args or {})
        self._validate_args(spec, args)
        actual_run_id = run_id or _new_run_id(spec.name)
        if self.store.exists(actual_run_id):
            raise WorkflowStateError(f"workflow run already exists: {actual_run_id}")
        state = WorkflowRunState.create(run_id=actual_run_id, spec=spec, args=args)
        self.store.write(state)
        return await self._execute(spec, state, resume=False, background=background)

    async def resume(self, *, run_id: str, background: bool = False) -> WorkflowRunState:
        state = self.store.read(run_id)
        if state.status == RunStatus.COMPLETED:
            return state
        spec = WorkflowSpec.model_validate(state.spec)
        return await self._execute(spec, state, resume=True, background=background)

    def status(self, run_id: str) -> WorkflowRunState:
        return self.store.read(run_id)

    async def cancel(self, run_id: str) -> WorkflowRunState:
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        state = self.store.read(run_id)
        if state.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            state.status = RunStatus.CANCELLED
            self.store.write(state)
            await self.tape.task_cancelled(state)
        return state

    async def cancel_all(self) -> None:
        for run_id in list(self._tasks):
            await self.cancel(run_id)

    async def _execute(
        self,
        spec: WorkflowSpec,
        state: WorkflowRunState,
        *,
        resume: bool,
        background: bool,
    ) -> WorkflowRunState:
        runtime = WorkflowRuntime(runner=self.runner, store=self.store, tape=self.tape)
        if not background:
            return await runtime.execute(spec, state, resume=resume)

        task = asyncio.create_task(runtime.execute(spec, state, resume=resume))
        self._tasks[state.run_id] = task

        def cleanup(done: asyncio.Task[WorkflowRunState]) -> None:
            del done
            self._tasks.pop(state.run_id, None)

        task.add_done_callback(cleanup)
        return self.store.read(state.run_id)

    @staticmethod
    def _validate_args(spec: WorkflowSpec, args: dict[str, Any]) -> None:
        if spec.args_schema is None:
            return
        try:
            validate_json_schema(instance=args, schema=spec.args_schema)
        except JsonSchemaValidationError as exc:
            raise WorkflowExecutionError(f"workflow args failed schema validation: {exc.message}") from exc


def _new_run_id(name: str) -> str:
    return f"{name}-{uuid.uuid4().hex[:8]}"


def spec_from_inputs(spec: dict[str, Any] | None = None, spec_path: str | None = None) -> WorkflowSpec:
    if spec is not None and spec_path is not None:
        raise WorkflowExecutionError("provide either spec or spec_path, not both")
    if spec is not None:
        return load_workflow_spec(spec)
    if spec_path is not None:
        return load_workflow_spec_file(spec_path)
    raise WorkflowExecutionError("workflow spec or spec_path is required")
