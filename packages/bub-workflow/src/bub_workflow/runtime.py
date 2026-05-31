from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.date import DateTrigger
from republic import ToolContext

from bub_workflow.config import WorkflowSettings
from bub_workflow.jobs import run_bee_node
from bub_workflow.models import (
    BeeNodeInput,
    BeeProjection,
    BeeStartInput,
    BeeTemplateInput,
    topological_node_ids,
)
from bub_workflow.projection import BeeProjectionStore, utc_now
from bub_workflow.tape import WorkflowTape
from bub_workflow.templates import resolve_template


class BeeRuntime:
    def __init__(
        self,
        *,
        scheduler: BaseScheduler,
        context: ToolContext,
        workspace: str | Path,
        settings: WorkflowSettings,
    ) -> None:
        self.scheduler = scheduler
        self.context = context
        self.settings = settings
        self.store = BeeProjectionStore(workspace, settings)
        self.tape = WorkflowTape(context)

    async def start(self, params: BeeStartInput) -> BeeProjection:
        self._start_scheduler()
        resolved = resolve_template(
            params,
            workspace=self.store.workspace,
            settings=self.settings,
            state=self.context.state,
        )
        run_id = params.run_id or uuid.uuid4().hex[:12]
        projection = self.store.create(
            run_id,
            params,
            resolved.template,
            resolved.source,
            resolved.inputs,
        )
        self.store.write(projection)
        await self.tape.task_started(projection)

        if params.execute:
            await self._execute_ready_nodes(resolved.template, projection)

        return projection

    async def step(self, run_id: str) -> BeeProjection:
        self._start_scheduler()
        projection = self.store.read(run_id)
        template = template_from_projection(projection)
        await self._execute_ready_nodes(template, projection)
        return projection

    def status(self, run_id: str) -> BeeProjection:
        return self.store.read(run_id)

    def _start_scheduler(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    async def _execute_ready_nodes(
        self,
        template: BeeTemplateInput,
        projection: BeeProjection,
    ) -> None:
        if projection.status in {"completed", "failed"}:
            return

        projection.status = "running"
        self.store.write(projection)

        try:
            while ready := ready_nodes(template, projection):
                outputs = completed_outputs(projection)
                await self._run_node_batch(projection, ready, outputs)

            finish_if_terminal(template, projection)
            self.store.write(projection)
            if projection.status == "completed":
                await self.tape.task_finished(projection)
            elif projection.status == "failed":
                await self.tape.task_failed(projection)
        except Exception as exc:
            projection.status = "failed"
            projection.error = str(exc)
            projection.finished_at = utc_now()
            self.store.write(projection)
            await self.tape.task_failed(projection)
            raise

    async def _run_node_batch(
        self,
        projection: BeeProjection,
        nodes: list[BeeNodeInput],
        outputs: dict[str, Any],
    ) -> None:
        batch = _JobBatch(self.scheduler)
        try:
            for node in nodes:
                prompt = render_prompt(
                    node.prompt or "",
                    brief=projection.brief,
                    inputs=projection.inputs,
                    nodes=outputs,
                )
                job_id = job_id_for(projection.run_id, node.id)
                item = projection.nodes[node.id]
                item.prompt = prompt or item.prompt
                item.status = "running"
                item.started_at = utc_now()
                self.store.write(projection)
                await self.tape.agent_started(projection, item)

                batch.expect(job_id)
                schedule_node_job(
                    scheduler=self.scheduler,
                    job_id=job_id,
                    run_id=projection.run_id,
                    node=node,
                    prompt=prompt,
                    context=self.context,
                    inputs=projection.inputs,
                    outputs=outputs,
                )
            events = await batch.wait()
        finally:
            batch.close()

        for node in nodes:
            update_node_from_event(projection, node, events[job_id_for(projection.run_id, node.id)])
            self.store.write(projection)
            await self.tape.agent_finished(projection, projection.nodes[node.id])
            await self.tape.checkpoint(projection)


def schedule_node_job(
    *,
    scheduler: BaseScheduler,
    job_id: str,
    run_id: str,
    node: BeeNodeInput,
    prompt: str,
    context: ToolContext,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
) -> None:
    try:
        scheduler.add_job(
            run_bee_node,
            trigger=DateTrigger(run_date=datetime.now(UTC)),
            id=job_id,
            kwargs={
                "node": node.model_dump(mode="json"),
                "prompt": prompt,
                "run_id": run_id,
                "state": {
                    **dict(context.state),
                    "_workflow_run_id": run_id,
                    "_workflow_tape_name": context.tape,
                },
                "tape_name": context.tape,
                "inputs": inputs,
                "outputs": outputs,
            },
            coalesce=True,
            max_instances=1,
        )
    except ConflictingIdError as exc:
        raise RuntimeError(f"bee job id already exists: {job_id}") from exc


def ready_nodes(template: BeeTemplateInput, projection: BeeProjection) -> list[BeeNodeInput]:
    return [
        template.node_map[node_id]
        for node_id in topological_node_ids(template)
        if _node_ready(template.node_map[node_id], projection)
    ]


def completed_outputs(projection: BeeProjection) -> dict[str, Any]:
    return {
        node_id: node.output
        for node_id, node in projection.nodes.items()
        if node.status == "completed"
    }


def finish_if_terminal(template: BeeTemplateInput, projection: BeeProjection) -> None:
    failed = [node for node in projection.nodes.values() if node.status == "failed"]
    if failed:
        skip_blocked_nodes(template, projection, {node.id for node in failed})
        projection.status = "failed"
        projection.error = "; ".join(f"{node.id}: {node.error}" for node in failed)
        projection.finished_at = utc_now()
        return

    if all(node.status == "completed" for node in projection.nodes.values()):
        projection.status = "completed"
        projection.result = {node_id: node.output for node_id, node in projection.nodes.items()}
        projection.finished_at = utc_now()


def skip_blocked_nodes(
    template: BeeTemplateInput,
    projection: BeeProjection,
    failed_ids: set[str],
) -> None:
    for node_id in topological_node_ids(template):
        node = projection.nodes[node_id]
        if node.status != "pending":
            continue
        blocked_by = sorted(set(node.depends_on) & failed_ids)
        if not blocked_by:
            continue
        node.status = "skipped"
        node.error = f"blocked by failed dependency: {', '.join(blocked_by)}"
        node.finished_at = utc_now()
        failed_ids.add(node.id)


def update_node_from_event(
    projection: BeeProjection,
    node: BeeNodeInput,
    event: JobExecutionEvent,
) -> None:
    item = projection.nodes[node.id]
    item.finished_at = utc_now()
    if event.exception:
        item.status = "failed"
        item.error = str(event.exception)
    else:
        item.status = "completed"
        item.output = event.retval


def template_from_projection(projection: BeeProjection) -> BeeTemplateInput:
    return BeeTemplateInput(
        name=projection.template_name,
        description=projection.description,
        skill=projection.skill,
        config=projection.config,
        nodes=[
            BeeNodeInput(
                id=node.id,
                title=node.title,
                description=node.description,
                prompt=node.prompt,
                depends_on=node.depends_on,
                executor=cast(Any, node.executor),
                call=node.call,
                model=node.model,
                allowed_tools=node.allowed_tools,
                allowed_skills=node.allowed_skills,
                output_schema=node.output_schema,
                features=node.features,
            )
            for node in projection.nodes.values()
        ],
    )


def render_prompt(
    template: str,
    *,
    brief: str,
    inputs: dict[str, Any],
    nodes: dict[str, Any],
) -> str:
    context = {"brief": brief, "inputs": inputs, "nodes": nodes}

    def replace(match: re.Match[str]) -> str:
        value = resolve_path(context, match.group("path").split("."))
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    return TEMPLATE_REF.sub(replace, template)


def resolve_path(context: dict[str, Any], path: list[str]) -> Any:
    value: Any = context
    for part in path:
        if isinstance(value, dict) and part in value:
            value = value[part]
            continue
        raise RuntimeError(f"bee prompt reference not found: {'.'.join(path)}")
    return value


def job_id_for(run_id: str, node_id: str) -> str:
    return f"bee:{run_id}:{job_id_part(node_id)}"


def job_id_part(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in "._-" else "-" for char in value)
    return normalized.strip("-") or "node"


def _node_ready(node: BeeNodeInput, projection: BeeProjection) -> bool:
    if projection.nodes[node.id].status != "pending":
        return False
    return all(
        projection.nodes[dependency_id].status == "completed"
        for dependency_id in node.depends_on
    )


class _JobBatch:
    def __init__(self, scheduler: BaseScheduler) -> None:
        self.scheduler = scheduler
        self.loop = asyncio.get_running_loop()
        self.futures: dict[str, asyncio.Future[JobExecutionEvent]] = {}
        self.scheduler.add_listener(self._listen, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    def expect(self, job_id: str) -> None:
        self.futures[job_id] = self.loop.create_future()

    async def wait(self) -> dict[str, JobExecutionEvent]:
        events = await asyncio.gather(*self.futures.values())
        return dict(zip(self.futures, events, strict=True))

    def close(self) -> None:
        self.scheduler.remove_listener(self._listen)

    def _listen(self, event: JobExecutionEvent) -> None:
        future = self.futures.get(event.job_id)
        if future is None or future.done():
            return
        self.loop.call_soon_threadsafe(future.set_result, event)


TEMPLATE_REF = re.compile(
    r"\{(?P<path>(?:brief|inputs|nodes)(?:\.[A-Za-z0-9_-]+)*)\}"
)
