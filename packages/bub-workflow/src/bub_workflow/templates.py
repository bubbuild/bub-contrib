from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema
from pydantic import ValidationError

from bub_workflow.config import WorkflowSettings, resolve_workspace_path
from bub_workflow.constants import WORKFLOW_TEMPLATES_STATE_KEY
from bub_workflow.models import BeeStartInput, BeeTemplateInput


YAML_TEMPLATE_SUFFIXES = {".yaml", ".yml"}
WORKFLOW_TEMPLATE_FILE = "workflow.yaml"


@dataclass(frozen=True)
class ResolvedTemplate:
    template: BeeTemplateInput
    source: str | None
    inputs: dict[str, Any]


def resolve_template(
    params: BeeStartInput,
    *,
    workspace: str | Path,
    settings: WorkflowSettings,
    state: dict[str, Any],
) -> ResolvedTemplate:
    if params.template.is_inline:
        template = params.template.inline_template()
        source = None
    else:
        template = load_template(
            params.template.name,
            workspace=workspace,
            settings=settings,
            state=state,
        )
        source = params.template.name
    return ResolvedTemplate(
        template=template,
        source=source,
        inputs=resolve_inputs(template=template, values=params.template.inputs),
    )


def resolve_inputs(
    *,
    template: BeeTemplateInput,
    values: dict[str, Any],
) -> dict[str, Any]:
    inputs = _with_defaults(template.input_schema, values)
    if not template.input_schema:
        return inputs

    schema = {
        "type": "object",
        "properties": template.input_schema,
        "required": [
            name
            for name, item in template.input_schema.items()
            if "default" not in item
        ],
        "additionalProperties": False,
    }
    try:
        validate_json_schema(instance=inputs, schema=schema)
    except JsonSchemaValidationError as exc:
        message = f"bee template inputs failed validation: {exc.message}"
        raise RuntimeError(message) from exc
    return inputs


def load_template(
    name: str,
    *,
    workspace: str | Path,
    settings: WorkflowSettings,
    state: dict[str, Any],
) -> BeeTemplateInput:
    if template := _template_from_state(name, state):
        return template

    for path in _candidate_paths(name, Path(workspace), settings):
        if path.is_file():
            return _read_yaml_template(path)

    raise RuntimeError(f"bee template not found: {name}")


def _with_defaults(
    input_schema: dict[str, dict[str, Any]],
    values: dict[str, Any],
) -> dict[str, Any]:
    inputs = {
        name: schema["default"]
        for name, schema in input_schema.items()
        if "default" in schema
    }
    inputs.update(values)
    return inputs


def _template_from_state(name: str, state: dict[str, Any]) -> BeeTemplateInput | None:
    registry = state.get(WORKFLOW_TEMPLATES_STATE_KEY)
    if not isinstance(registry, dict) or name not in registry:
        return None
    value = registry[name]
    if isinstance(value, BeeTemplateInput):
        return value
    if isinstance(value, dict):
        return BeeTemplateInput.model_validate(value)
    raise RuntimeError(f"bee template registry value is not a template: {name}")


def _candidate_paths(
    name: str,
    workspace: Path,
    settings: WorkflowSettings,
) -> list[Path]:
    path = Path(name).expanduser()
    if path.is_absolute() or path.parts != (path.name,):
        base = path if path.is_absolute() else workspace / path
        if base.suffix:
            return [base] if base.suffix in YAML_TEMPLATE_SUFFIXES else []
        return [base / WORKFLOW_TEMPLATE_FILE]

    template_dirs = [
        resolve_workspace_path(workspace, template_dir)
        for template_dir in settings.template_dirs
    ]
    if path.suffix:
        if path.suffix not in YAML_TEMPLATE_SUFFIXES:
            return []
        return [template_dir / path for template_dir in template_dirs]

    candidates: list[Path] = []
    for template_dir in template_dirs:
        candidates.append(template_dir / name / WORKFLOW_TEMPLATE_FILE)
    return candidates


def _read_yaml_template(path: Path) -> BeeTemplateInput:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"bee template yaml must contain a mapping: {path}")
        return BeeTemplateInput.model_validate(payload)
    except OSError as exc:
        raise RuntimeError(f"failed to read bee template: {path}") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"bee template is not valid yaml: {path}") from exc
    except ValidationError as exc:
        raise RuntimeError(f"bee template has invalid shape: {path}: {exc}") from exc
