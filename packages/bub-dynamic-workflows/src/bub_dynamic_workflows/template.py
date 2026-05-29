from __future__ import annotations

import json
import re
from typing import Any

from bub_dynamic_workflows.errors import WorkflowExecutionError

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*(?:\[[0-9]+\])?)\}")


def render_template(text: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        value = resolve_reference(match.group(1), context)
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    return PLACEHOLDER_RE.sub(replace, text)


def resolve_reference(reference: str, context: dict[str, Any]) -> Any:
    current: Any = context
    for part in reference.split("."):
        name, index = _split_index(part)
        if not isinstance(current, dict) or name not in current:
            raise WorkflowExecutionError(f"unknown workflow reference: {reference}")
        current = current[name]
        if index is None:
            continue
        if not isinstance(current, list):
            raise WorkflowExecutionError(f"workflow reference is not a list: {reference}")
        try:
            current = current[index]
        except IndexError as exc:
            raise WorkflowExecutionError(f"workflow reference index is out of range: {reference}") from exc
    return current


def _split_index(part: str) -> tuple[str, int | None]:
    if "[" not in part:
        return part, None
    name, _, rest = part.partition("[")
    try:
        return name, int(rest.removesuffix("]"))
    except ValueError as exc:
        raise WorkflowExecutionError(f"invalid workflow reference index: {part}") from exc
