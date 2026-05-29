from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from bub_dynamic_workflows.errors import WorkflowSpecError

NODE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
TEMPLATE_METADATA_PATHS = (
    Path("assets/metadata.json"),
    Path("assets/workflow.json"),
    Path("assets/workflow.yaml"),
    Path("assets/workflow.yml"),
    Path("workflow.json"),
    Path("workflow.yaml"),
    Path("workflow.yml"),
    Path("workflow.toml"),
)


class WorkflowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=NODE_ID_PATTERN)
    prompt: str
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    model: str | None = None
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    output_schema: dict[str, Any] | None = None
    acceptance: list[str] = Field(default_factory=list)
    foreach: str | None = None
    concurrency: int | None = Field(default=None, ge=1, le=16)

    @field_validator("prompt")
    @classmethod
    def _prompt_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("node prompt must not be empty")
        return value

    @field_validator("depends_on")
    @classmethod
    def _dedupe_dependencies(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NODE_ID_PATTERN)
    description: str
    profile: Literal["bee"] = "bee"
    args_schema: dict[str, Any] | None = None
    concurrency: int = Field(default=4, ge=1, le=16)
    budget: int | None = Field(default=None, ge=1)
    nodes: list[WorkflowNode]

    @field_validator("description")
    @classmethod
    def _description_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workflow description must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_node_references(self) -> WorkflowSpec:
        if not self.nodes:
            raise ValueError("workflow must define at least one node")

        node_ids = [node.id for node in self.nodes]
        duplicate_ids = sorted({node_id for node_id in node_ids if node_ids.count(node_id) > 1})
        if duplicate_ids:
            raise ValueError(f"duplicate node id(s): {', '.join(duplicate_ids)}")

        known_ids = set(node_ids)
        for node in self.nodes:
            if node.id in node.depends_on:
                raise ValueError(f"node '{node.id}' cannot depend on itself")
            missing = sorted(set(node.depends_on) - known_ids)
            if missing:
                raise ValueError(f"node '{node.id}' depends on unknown node(s): {', '.join(missing)}")
        return self

    @property
    def node_map(self) -> dict[str, WorkflowNode]:
        return {node.id: node for node in self.nodes}


def load_workflow_spec(value: dict[str, Any]) -> WorkflowSpec:
    try:
        spec = WorkflowSpec.model_validate(value)
    except ValidationError as exc:
        raise WorkflowSpecError(str(exc)) from exc
    from bub_dynamic_workflows.graph import validate_acyclic_graph

    validate_acyclic_graph(spec)
    return spec


def load_workflow_spec_file(path: str | Path) -> WorkflowSpec:
    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir():
        resolved = _find_template_metadata(resolved)
    if not resolved.is_file():
        raise WorkflowSpecError(f"workflow spec file does not exist: {resolved}")
    return load_workflow_spec(load_mapping_file(resolved))


def load_mapping_file(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    suffix = resolved.suffix.lower()
    try:
        if suffix == ".json":
            value = json.loads(resolved.read_text(encoding="utf-8"))
        elif suffix == ".toml":
            value = tomllib.loads(resolved.read_text(encoding="utf-8"))
        elif suffix in {".yaml", ".yml"}:
            value = _load_yaml(resolved)
        else:
            raise WorkflowSpecError(f"unsupported workflow file extension: {suffix or '(none)'}")
    except OSError as exc:
        raise WorkflowSpecError(f"failed to read workflow file: {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowSpecError(f"invalid json workflow file: {resolved}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise WorkflowSpecError(f"invalid toml workflow file: {resolved}: {exc}") from exc

    if not isinstance(value, dict):
        raise WorkflowSpecError(f"workflow file must contain an object: {resolved}")
    return value


def _find_template_metadata(template_dir: Path) -> Path:
    for candidate in TEMPLATE_METADATA_PATHS:
        path = template_dir / candidate
        if path.is_file():
            return path
    checked = ", ".join(str(path) for path in TEMPLATE_METADATA_PATHS)
    raise WorkflowSpecError(f"workflow template has no metadata file; checked: {checked}")


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise WorkflowSpecError("pyyaml is required to load YAML workflow files") from exc
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise WorkflowSpecError(f"invalid yaml workflow file: {path}: {exc}") from exc
