from __future__ import annotations

import asyncio
import json
import threading
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, ClassVar

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema
from redun import Scheduler, task
from redun.backends.db import RedunBackendDb
from redun.config import create_config_section
from redun.executors.local import LocalExecutor

from bub_dynamic_workflows.errors import WorkflowExecutionError
from bub_dynamic_workflows.graph import topological_node_ids
from bub_dynamic_workflows.runner import WorkflowRunner
from bub_dynamic_workflows.spec import WorkflowNode, WorkflowSpec
from bub_dynamic_workflows.state import NodeAttempt, NodeStatus, RunStatus, WorkflowProjectionStore, WorkflowRunState, utc_now
from bub_dynamic_workflows.tape import WorkflowTape
from bub_dynamic_workflows.template import render_template, resolve_reference

redun_namespace = "bub_dynamic_workflows"


class WorkflowRuntime:
    def __init__(self, *, runner: WorkflowRunner, store: WorkflowProjectionStore, tape: WorkflowTape) -> None:
        self.runner = runner
        self.store = store
        self.tape = tape

    async def execute(self, spec: WorkflowSpec, state: WorkflowRunState, *, resume: bool = False) -> WorkflowRunState:
        state.status = RunStatus.RUNNING
        state.started_at = state.started_at or utc_now()
        self.store.write(state)
        if resume:
            await self.tape.task_resumed(state)
        else:
            await self.tape.task_started(state)

        context = _RedunWorkflowContext(
            runner=self.runner,
            store=self.store,
            tape=self.tape,
            spec=spec,
            run_id=state.run_id,
            loop=asyncio.get_running_loop(),
        )
        context_id = _WorkflowContextRegistry.register(context)
        context.context_id = context_id
        try:
            scheduler = self._scheduler(spec, state.run_id)
            expression = _build_workflow_expression(context_id, spec)
            try:
                await asyncio.to_thread(
                    scheduler.run,
                    expression,
                    cache=False,
                    execution_id=f"{state.run_id}-{uuid.uuid4().hex[:8]}",
                )
            except asyncio.CancelledError:
                context.cancel()
                state = self.store.read(state.run_id)
                state.status = RunStatus.CANCELLED
                state.finished_at = utc_now()
                self.store.write(state)
                await self.tape.task_cancelled(state)
                raise
            except Exception as exc:
                state = self.store.read(state.run_id)
                await self._mark_blocked_nodes_skipped(spec, state)
                if not any(node.status == NodeStatus.FAILED for node in state.nodes.values()):
                    state.status = RunStatus.FAILED
                    state.error = str(exc)
                    state.finished_at = utc_now()
                    self.store.write(state)
                    await self.tape.task_failed(state, state.error)
                    raise WorkflowExecutionError(str(exc)) from exc
        finally:
            _WorkflowContextRegistry.unregister(context_id)

        state = self.store.read(state.run_id)
        await self._mark_blocked_nodes_skipped(spec, state)
        if any(node.status == NodeStatus.FAILED for node in state.nodes.values()):
            state.status = RunStatus.FAILED
            state.error = "one or more workflow nodes failed"
            await self.tape.task_failed(state, state.error)
        else:
            state.status = RunStatus.COMPLETED
            state.error = None
            await self.tape.task_finished(state)
        state.finished_at = utc_now()
        self.store.write(state)
        return state

    def _scheduler(self, spec: WorkflowSpec, run_id: str) -> Scheduler:
        db_path = self.store.root / run_id / "redun.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        executor_config = create_config_section({"max_workers": str(spec.concurrency), "mode": "thread"})
        executor = LocalExecutor("default", config=executor_config)
        backend = RedunBackendDb(db_uri=f"sqlite:///{db_path}")
        backend.load(migrate=True)
        return Scheduler(backend=backend, executor=executor)

    async def _mark_blocked_nodes_skipped(self, spec: WorkflowSpec, state: WorkflowRunState) -> None:
        changed = False
        for node_id in topological_node_ids(spec):
            node = spec.node_map[node_id]
            node_state = state.nodes[node.id]
            if node_state.status != NodeStatus.PENDING:
                continue
            blocked = [
                dependency_id
                for dependency_id in node.depends_on
                if state.nodes[dependency_id].status in {NodeStatus.FAILED, NodeStatus.SKIPPED}
            ]
            if not blocked:
                continue
            node_state.status = NodeStatus.SKIPPED
            node_state.error = f"blocked by dependency failure: {', '.join(blocked)}"
            node_state.finished_at = utc_now()
            state.checkpoint_seq += 1
            changed = True
            await self.tape.node_skipped(state, node, node_state.error)
        if changed:
            self.store.write(state)
            await self.tape.checkpoint(state)


@dataclass
class _RedunWorkflowContext:
    runner: WorkflowRunner
    store: WorkflowProjectionStore
    tape: WorkflowTape
    spec: WorkflowSpec
    run_id: str
    loop: asyncio.AbstractEventLoop
    context_id: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)
    cancelled: threading.Event = field(default_factory=threading.Event)
    active_futures: set[Future[Any]] = field(default_factory=set)
    foreach_semaphores: dict[str, threading.Semaphore] = field(default_factory=dict)

    def cancel(self) -> None:
        self.cancelled.set()
        for future in list(self.active_futures):
            future.cancel()

    def run_node(self, node_data: dict[str, Any], dependency_outputs: dict[str, Any]) -> Any:
        if self.cancelled.is_set():
            raise _WorkflowCancelled()

        node = WorkflowNode.model_validate(node_data)
        if node.foreach is not None:
            return self.run_foreach_node(node, node_data, dependency_outputs)

        completed_output = self._completed_output(node)
        if completed_output is not _MISSING:
            return completed_output

        state, attempt_index = self._mark_started(node)
        self._run_async(self.tape.node_started(state, node, attempt_index))

        try:
            prompt = self._prompt(state, node, item=None, index=None, dependency_outputs=dependency_outputs)
            output = self._run_async(
                self.runner.run_node(prompt=prompt, node=node, run_id=self.run_id, attempt=attempt_index)
            )
            output = self._validated_output(node, output)
        except _WorkflowCancelled:
            raise
        except Exception as exc:
            self._mark_failed(node, attempt_index, str(exc))
            raise _WorkflowNodeFailed(node.id) from exc

        state = self._mark_finished(node, attempt_index, output)
        self._run_async(self.tape.node_finished(state, node, output))
        self._run_async(self.tape.checkpoint(state))
        return output

    def run_foreach_node(
        self,
        node: WorkflowNode,
        node_data: dict[str, Any],
        dependency_outputs: dict[str, Any],
    ) -> Any:
        completed_output = self._completed_output(node)
        if completed_output is not _MISSING:
            return completed_output

        state = self._mark_foreach_started(node)
        self._run_async(self.tape.node_started(state, node, 0))
        items = self._foreach_items(node, state, dependency_outputs)
        if items is None:
            raise WorkflowExecutionError(f"foreach node has no foreach reference: {node.id}")

        item_outputs = [
            _redun_workflow_foreach_item(self.context_id, node_data, index, item, dependency_outputs)
            for index, item in enumerate(items)
        ]
        return _redun_workflow_foreach_collect(self.context_id, node_data, item_outputs)

    def run_foreach_item(
        self,
        node_data: dict[str, Any],
        item_index: int,
        item: Any,
        dependency_outputs: dict[str, Any],
    ) -> Any:
        if self.cancelled.is_set():
            raise _WorkflowCancelled()

        node = WorkflowNode.model_validate(node_data)
        completed_output = self._completed_item_output(node, item_index)
        if completed_output is not _MISSING:
            return completed_output

        state, attempt_index = self._mark_item_started(node, item_index, item)

        try:
            with self._foreach_semaphore(node):
                prompt = self._prompt(state, node, item=item, index=item_index, dependency_outputs=dependency_outputs)
                output = self._run_async(
                    self.runner.run_node(
                        prompt=prompt,
                        node=node,
                        run_id=self.run_id,
                        attempt=attempt_index,
                        item_index=item_index,
                    )
                )
            output = self._validated_output(node, output)
        except _WorkflowCancelled:
            raise
        except Exception as exc:
            self._mark_failed(node, attempt_index, str(exc))
            raise _WorkflowNodeFailed(f"{node.id}[{item_index}]") from exc

        state = self._mark_item_finished(node, attempt_index, output)
        self._run_async(self.tape.checkpoint(state))
        return output

    def collect_foreach_node(self, node_data: dict[str, Any], outputs: list[Any]) -> Any:
        if self.cancelled.is_set():
            raise _WorkflowCancelled()

        node = WorkflowNode.model_validate(node_data)
        state = self._mark_finished(node, attempt_index=None, output=outputs)
        self._run_async(self.tape.node_finished(state, node, outputs))
        self._run_async(self.tape.checkpoint(state))
        return outputs

    def _completed_output(self, node: WorkflowNode) -> Any:
        with self.lock:
            state = self.store.read(self.run_id)
            node_state = state.nodes[node.id]
            if node_state.status == NodeStatus.COMPLETED:
                return node_state.output
            return _MISSING

    def _foreach_semaphore(self, node: WorkflowNode) -> threading.Semaphore:
        with self.lock:
            semaphore = self.foreach_semaphores.get(node.id)
            if semaphore is None:
                semaphore = threading.Semaphore(node.concurrency or self.spec.concurrency)
                self.foreach_semaphores[node.id] = semaphore
            return semaphore

    def _completed_item_output(self, node: WorkflowNode, item_index: int) -> Any:
        with self.lock:
            state = self.store.read(self.run_id)
            node_state = state.nodes[node.id]
            for attempt in node_state.attempts:
                if attempt.item_index == item_index and attempt.status == NodeStatus.COMPLETED:
                    return attempt.output
            return _MISSING

    def _mark_started(self, node: WorkflowNode) -> tuple[WorkflowRunState, int]:
        with self.lock:
            state = self.store.read(self.run_id)
            node_state = state.nodes[node.id]
            attempt_index = node_state.next_attempt
            attempt = NodeAttempt(index=attempt_index, started_at=utc_now())
            node_state.status = NodeStatus.RUNNING
            node_state.started_at = node_state.started_at or attempt.started_at
            node_state.attempts.append(attempt)
            self.store.write(state)
            return state, attempt_index

    def _mark_foreach_started(self, node: WorkflowNode) -> WorkflowRunState:
        with self.lock:
            state = self.store.read(self.run_id)
            node_state = state.nodes[node.id]
            node_state.status = NodeStatus.RUNNING
            node_state.started_at = node_state.started_at or utc_now()
            self.store.write(state)
            return state

    def _mark_item_started(self, node: WorkflowNode, item_index: int, item: Any) -> tuple[WorkflowRunState, int]:
        with self.lock:
            state = self.store.read(self.run_id)
            node_state = state.nodes[node.id]
            attempt_index = node_state.next_attempt
            attempt = NodeAttempt(
                index=attempt_index,
                item_index=item_index,
                item=item,
                started_at=utc_now(),
            )
            node_state.status = NodeStatus.RUNNING
            node_state.started_at = node_state.started_at or attempt.started_at
            node_state.attempts.append(attempt)
            self.store.write(state)
            return state, attempt_index

    def _mark_finished(self, node: WorkflowNode, attempt_index: int | None, output: Any) -> WorkflowRunState:
        with self.lock:
            state = self.store.read(self.run_id)
            node_state = state.nodes[node.id]
            if attempt_index is not None:
                attempt = node_state.attempts[attempt_index - 1]
                attempt.status = NodeStatus.COMPLETED
                attempt.output = output
                attempt.finished_at = utc_now()
            node_state.status = NodeStatus.COMPLETED
            node_state.output = output
            node_state.error = None
            node_state.finished_at = utc_now()
            state.checkpoint_seq += 1
            self.store.write(state)
            return state

    def _mark_item_finished(self, node: WorkflowNode, attempt_index: int, output: Any) -> WorkflowRunState:
        with self.lock:
            state = self.store.read(self.run_id)
            node_state = state.nodes[node.id]
            attempt = node_state.attempts[attempt_index - 1]
            attempt.status = NodeStatus.COMPLETED
            attempt.output = output
            attempt.finished_at = utc_now()
            state.checkpoint_seq += 1
            self.store.write(state)
            return state

    def _mark_failed(self, node: WorkflowNode, attempt_index: int, error: str) -> WorkflowRunState:
        with self.lock:
            state = self.store.read(self.run_id)
            node_state = state.nodes[node.id]
            attempt = node_state.attempts[attempt_index - 1]
            attempt.status = NodeStatus.FAILED
            attempt.error = error
            attempt.finished_at = utc_now()
            node_state.status = NodeStatus.FAILED
            node_state.error = error
            node_state.finished_at = utc_now()
            state.checkpoint_seq += 1
            self.store.write(state)
        self._run_async(self.tape.node_failed(state, node, error))
        self._run_async(self.tape.checkpoint(state))
        return state

    def _foreach_items(
        self,
        node: WorkflowNode,
        state: WorkflowRunState,
        dependency_outputs: dict[str, Any],
    ) -> list[Any] | None:
        if node.foreach is None:
            return None
        value = resolve_reference(
            node.foreach,
            self._context(state, item=None, index=None, dependency_outputs=dependency_outputs),
        )
        if not isinstance(value, list):
            raise WorkflowExecutionError(f"foreach reference must resolve to a list: {node.foreach}")
        return value

    def _prompt(
        self,
        state: WorkflowRunState,
        node: WorkflowNode,
        *,
        item: Any | None,
        index: int | None,
        dependency_outputs: dict[str, Any],
    ) -> str:
        task_prompt = render_template(
            node.prompt,
            self._context(state, item=item, index=index, dependency_outputs=dependency_outputs),
        )
        blocks = [f"Workflow: {self.spec.name}", f"Description: {self.spec.description}", f"Node: {node.id}"]
        if node.description:
            blocks.append(f"Node description: {node.description}")
        if node.acceptance:
            blocks.append("Acceptance criteria:\n" + "\n".join(f"- {entry}" for entry in node.acceptance))
        if node.output_schema is not None:
            blocks.append(
                "Output contract:\n"
                "Return only JSON that satisfies this schema:\n"
                + json.dumps(node.output_schema, ensure_ascii=False, indent=2)
            )
        blocks.append("Task prompt:\n" + task_prompt)
        return "\n\n".join(blocks)

    @staticmethod
    def _context(
        state: WorkflowRunState,
        *,
        item: Any | None,
        index: int | None,
        dependency_outputs: dict[str, Any],
    ) -> dict[str, Any]:
        node_outputs = {
            node_id: node.output
            for node_id, node in state.nodes.items()
            if node.status == NodeStatus.COMPLETED
        }
        node_outputs.update(dependency_outputs)
        return {
            "args": state.args,
            "item": item,
            "index": index,
            "nodes": node_outputs,
        }

    @staticmethod
    def _validated_output(node: WorkflowNode, output: str) -> Any:
        if node.output_schema is None:
            return output
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise WorkflowExecutionError(f"node '{node.id}' returned invalid JSON") from exc
        try:
            validate_json_schema(instance=value, schema=node.output_schema)
        except JsonSchemaValidationError as exc:
            raise WorkflowExecutionError(f"node '{node.id}' output failed schema validation: {exc.message}") from exc
        return value

    def _run_async(self, awaitable: Any) -> Any:
        if self.cancelled.is_set():
            raise _WorkflowCancelled()
        future = asyncio.run_coroutine_threadsafe(awaitable, self.loop)
        self.active_futures.add(future)
        try:
            return future.result()
        finally:
            self.active_futures.discard(future)


class _WorkflowContextRegistry:
    _contexts: ClassVar[dict[str, _RedunWorkflowContext]] = {}
    _lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def register(cls, context: _RedunWorkflowContext) -> str:
        context_id = uuid.uuid4().hex
        with cls._lock:
            cls._contexts[context_id] = context
        return context_id

    @classmethod
    def unregister(cls, context_id: str) -> None:
        with cls._lock:
            cls._contexts.pop(context_id, None)

    @classmethod
    def get(cls, context_id: str) -> _RedunWorkflowContext:
        with cls._lock:
            return cls._contexts[context_id]


class _WorkflowCancelled(Exception):
    pass


class _WorkflowNodeFailed(Exception):
    pass


_MISSING = object()


@task(name="workflow_node", namespace=redun_namespace, cache=False, version="1")
def _redun_workflow_node(context_id: str, node_data: dict[str, Any], dependency_outputs: dict[str, Any]) -> Any:
    return _WorkflowContextRegistry.get(context_id).run_node(node_data, dependency_outputs)


@task(name="workflow_foreach_item", namespace=redun_namespace, cache=False, version="1")
def _redun_workflow_foreach_item(
    context_id: str,
    node_data: dict[str, Any],
    item_index: int,
    item: Any,
    dependency_outputs: dict[str, Any],
) -> Any:
    return _WorkflowContextRegistry.get(context_id).run_foreach_item(
        node_data,
        item_index,
        item,
        dependency_outputs,
    )


@task(name="workflow_foreach_collect", namespace=redun_namespace, cache=False, version="1")
def _redun_workflow_foreach_collect(context_id: str, node_data: dict[str, Any], outputs: list[Any]) -> Any:
    return _WorkflowContextRegistry.get(context_id).collect_foreach_node(node_data, outputs)


@task(name="workflow_root", namespace=redun_namespace, cache=False, version="1")
def _redun_workflow_root(outputs: dict[str, Any]) -> dict[str, Any]:
    return outputs


def _build_workflow_expression(context_id: str, spec: WorkflowSpec) -> Any:
    outputs: dict[str, Any] = {}
    node_map = spec.node_map
    for node_id in topological_node_ids(spec):
        node = node_map[node_id]
        dependencies = {dependency_id: outputs[dependency_id] for dependency_id in node.depends_on}
        outputs[node.id] = _redun_workflow_node(context_id, node.model_dump(mode="json"), dependencies)
    return _redun_workflow_root(outputs)
