from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from bub_workflow.config import WorkflowSettings, resolve_workspace_path
from bub_workflow.constants import WORKFLOW_PROJECTION_FILE
from bub_workflow.models import (
    BeeNodeInput,
    BeeNodeProjection,
    BeeProjection,
    BeeStartInput,
    BeeTemplateInput,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class BeeProjectionStore:
    def __init__(
        self,
        workspace: str | Path,
        settings: WorkflowSettings | None = None,
    ) -> None:
        settings = settings or WorkflowSettings()
        self.workspace = Path(workspace).expanduser().resolve()
        self.root = resolve_workspace_path(self.workspace, settings.projection_dir)

    def path_for(self, run_id: str) -> Path:
        return self.root / run_id / WORKFLOW_PROJECTION_FILE

    def create(
        self,
        run_id: str,
        params: BeeStartInput,
        template: BeeTemplateInput,
        template_source: str | None,
        inputs: dict[str, Any],
    ) -> BeeProjection:
        now = utc_now()
        return BeeProjection(
            run_id=run_id,
            template_name=template.name,
            template_source=template_source,
            description=template.description,
            brief=params.brief,
            inputs=inputs,
            skill=template.skill,
            config=template.config,
            nodes={node.id: self._node_projection(node) for node in template.nodes},
            created_at=now,
            updated_at=now,
        )

    def read(self, run_id: str) -> BeeProjection:
        path = self.path_for(run_id)
        try:
            return BeeProjection.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise RuntimeError(f"bee run does not exist: {run_id}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"bee projection is not valid json: {path}") from exc
        except ValidationError as exc:
            raise RuntimeError(f"bee projection has invalid shape: {path}: {exc}") from exc

    def write(self, projection: BeeProjection) -> Path:
        projection.updated_at = utc_now()
        path = self.path_for(projection.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(projection.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
        return path

    @staticmethod
    def finish(projection: BeeProjection, *, result: Any | None, error: str | None = None) -> None:
        projection.status = "failed" if error else "completed"
        projection.result = result
        projection.error = error
        projection.finished_at = utc_now()

    @staticmethod
    def _node_projection(node: BeeNodeInput) -> BeeNodeProjection:
        return BeeNodeProjection(
            id=node.id,
            title=node.title or node.id,
            description=node.description,
            depends_on=node.depends_on,
            executor=node.executor,
            call=node.call,
            prompt=node.prompt,
            model=node.model,
            allowed_tools=node.allowed_tools,
            allowed_skills=node.allowed_skills,
            output_schema=node.output_schema,
            features=node.features,
        )
