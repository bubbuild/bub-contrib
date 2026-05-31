from __future__ import annotations

import importlib
import inspect
import json
import re
from typing import Any

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate as validate_json_schema
from republic import ToolContext

from bub.builtin.tools import run_subagent
from bub_workflow.models import BeeNodeInput


async def run_bee_node(
    *,
    node: dict[str, Any],
    prompt: str | None,
    run_id: str,
    state: dict[str, Any],
    tape_name: str | None,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
) -> Any:
    parsed = BeeNodeInput.model_validate(node)
    if parsed.executor == "function":
        result = await _run_function_node(
            parsed,
            prompt=prompt,
            inputs=inputs,
            outputs=outputs,
            state=state,
        )
    else:
        result = await _run_subagent_node(
            parsed,
            prompt=prompt or "",
            run_id=run_id,
            state=state,
            tape_name=tape_name,
        )
    return _validated_output(node_id=parsed.id, output=result, output_schema=parsed.output_schema)


async def _run_function_node(
    node: BeeNodeInput,
    *,
    prompt: str | None,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    state: dict[str, Any],
) -> Any:
    target = node.call
    if target is None:
        raise RuntimeError(f"bee node '{node.id}' has no function call target")
    runner = state.get("_workflow_function_runner")
    if runner is not None:
        result = runner(
            node=node,
            prompt=prompt,
            inputs=inputs,
            outputs=outputs,
            state=state,
        )
    else:
        result = _import_callable(target)(
            node=node,
            prompt=prompt,
            inputs=inputs,
            outputs=outputs,
            state=state,
        )
    if inspect.isawaitable(result):
        result = await result
    return result


async def _run_subagent_node(
    node: BeeNodeInput,
    *,
    prompt: str,
    run_id: str,
    state: dict[str, Any],
    tape_name: str | None,
) -> str:
    context = ToolContext(
        tape=tape_name,
        run_id=f"bee:{run_id}:{_slug(node.id)}",
        state=state,
    )
    return await run_subagent.run(
        session=f"temp/bee-{run_id}-{_slug(node.id)}",
        prompt=prompt,
        model=node.model,
        allowed_tools=node.allowed_tools,
        allowed_skills=node.allowed_skills,
        context=context,
    )


def _import_callable(target: str) -> Any:
    module_name, _, attr = target.rpartition(":")
    if not module_name or not attr:
        module_name, _, attr = target.rpartition(".")
    if not module_name or not attr:
        raise RuntimeError(f"invalid function target: {target}")
    module = importlib.import_module(module_name)
    value: Any = module
    for part in attr.split("."):
        value = getattr(value, part)
    if not callable(value):
        raise RuntimeError(f"function target is not callable: {target}")
    return value


def _validated_output(*, node_id: str, output: Any, output_schema: dict[str, Any] | None) -> Any:
    if output_schema is None:
        return output
    value = output
    if isinstance(output, str):
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"bee node '{node_id}' returned invalid JSON") from exc
    try:
        validate_json_schema(instance=value, schema=output_schema)
    except JsonSchemaValidationError as exc:
        message = f"bee node '{node_id}' output failed schema validation: {exc.message}"
        raise RuntimeError(message) from exc
    return value


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "node"
