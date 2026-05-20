from __future__ import annotations

from typing import Any


def require_mapping(value: Any, *, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(message)
    return value


def required_text(value: Any, *, message: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(message)
    return text


def normalize_function_bindings(
    value: Any,
    *,
    message: str,
    missing_ok: bool,
) -> dict[str, str]:
    if value is None:
        if missing_ok:
            return {}
        raise RuntimeError(message)

    data = require_mapping(value, message=message)
    bindings: dict[str, str] = {}
    for operation, function_name in data.items():
        operation_text = required_text(
            operation,
            message="Extism functions must map operation names to export names",
        )
        function_text = required_text(
            function_name,
            message="Extism functions must map operation names to export names",
        )
        bindings[operation_text] = function_text
    return bindings
