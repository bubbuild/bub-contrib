from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bub_dynamic_workflows.errors import WorkflowStateError
from bub_dynamic_workflows.spec import WorkflowSpec

WORKFLOW_PROJECTION_FILE = "task.json"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    started_at: str
    item_index: int | None = None
    finished_at: str | None = None
    status: NodeStatus = NodeStatus.RUNNING
    item: Any | None = None
    output: Any | None = None
    error: str | None = None


class NodeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    status: NodeStatus = NodeStatus.PENDING
    attempts: list[NodeAttempt] = Field(default_factory=list)
    output: Any | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def next_attempt(self) -> int:
        return len(self.attempts) + 1


class WorkflowRunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    spec: dict[str, Any]
    args: dict[str, Any]
    status: RunStatus = RunStatus.PENDING
    nodes: dict[str, NodeState]
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    checkpoint_seq: int = 0
    error: str | None = None

    @classmethod
    def create(cls, *, run_id: str, spec: WorkflowSpec, args: dict[str, Any]) -> WorkflowRunState:
        return cls(
            run_id=run_id,
            spec=spec.model_dump(mode="json"),
            args=args,
            nodes={node.id: NodeState(id=node.id) for node in spec.nodes},
        )

    def touch(self) -> None:
        self.updated_at = utc_now()


class WorkflowProjectionStore:
    """Read and write the workspace status projection for a workflow run.

    The projection is intentionally human-readable and convenient for status/resume.
    Redun's database and Bub tape entries remain the execution evidence trail.
    """

    def __init__(self, workspace: str | Path) -> None:
        self.root = Path(workspace).expanduser().resolve() / ".bub" / "workflows"

    def path_for(self, run_id: str) -> Path:
        return self.root / run_id / WORKFLOW_PROJECTION_FILE

    def exists(self, run_id: str) -> bool:
        return self.path_for(run_id).is_file()

    def read(self, run_id: str) -> WorkflowRunState:
        path = self.path_for(run_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return WorkflowRunState.model_validate(value)
        except FileNotFoundError as exc:
            raise WorkflowStateError(f"workflow run does not exist: {run_id}") from exc
        except json.JSONDecodeError as exc:
            raise WorkflowStateError(f"workflow state is not valid json: {path}") from exc
        except ValidationError as exc:
            raise WorkflowStateError(f"workflow state has invalid shape: {path}: {exc}") from exc

    def write(self, state: WorkflowRunState) -> Path:
        path = self.path_for(state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        state.touch()
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
        return path
