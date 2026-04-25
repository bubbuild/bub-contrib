from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from bub.envelope import normalize_envelope
from republic import StreamEvent, TapeEntry
from republic.tape.entries import utc_now

BUB_EXTISM_ABI_VERSION = "bub.extism.v1"


class ExtismHookError(RuntimeError):
    pass


class ExtismHookSkip(Exception):
    pass


def build_request(hook_name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "abi_version": BUB_EXTISM_ABI_VERSION,
        "hook": hook_name,
        "args": to_json_value(args),
    }


def decode_response(raw_result: Any, *, hook_name: str) -> Any:
    if raw_result is None:
        raise ExtismHookSkip

    text = result_to_text(raw_result)
    if not text:
        raise ExtismHookSkip

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    if parsed is None:
        raise ExtismHookSkip
    if not isinstance(parsed, dict):
        return parsed

    if parsed.get("skip") is True:
        raise ExtismHookSkip
    if error := parsed.get("error"):
        if isinstance(error, dict):
            message = error.get("message", "Extism hook returned an error")
        else:
            message = str(error)
        raise ExtismHookError(str(message))
    if "value" in parsed:
        return parsed["value"]
    if hook_name in parsed:
        return parsed[hook_name]
    if "text" in parsed:
        return parsed["text"]
    return parsed


def result_to_text(raw_result: Any) -> str:
    if isinstance(raw_result, str):
        return raw_result
    if isinstance(raw_result, bytes):
        return raw_result.decode("utf-8")
    if isinstance(raw_result, bytearray):
        return bytes(raw_result).decode("utf-8")
    if isinstance(raw_result, memoryview):
        return raw_result.tobytes().decode("utf-8")
    return bytes(raw_result).decode("utf-8")


def to_json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {
            str(key): to_json_value(item)
            for key, item in value.items()
            if is_json_safe(item)
        }
    if isinstance(value, list | tuple):
        return [to_json_value(item) for item in value if is_json_safe(item)]
    if isinstance(value, StreamEvent):
        return {"kind": value.kind, "data": to_json_value(value.data)}
    if isinstance(value, TapeEntry):
        return tape_entry_to_dict(value)
    if is_dataclass(value):
        return to_json_value(asdict(value))
    if hasattr(value, "__dict__"):
        return to_json_value(normalize_envelope(value))
    return str(value)


def is_json_safe(value: Any) -> bool:
    try:
        json.dumps(to_json_value(value))
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def state_to_json(state: dict[str, Any]) -> dict[str, Any]:
    safe_state: dict[str, Any] = {}
    for key, value in state.items():
        if str(key).startswith("_runtime_"):
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        safe_state[str(key)] = to_json_value(value)
    return safe_state


def tape_entry_to_dict(entry: TapeEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "payload": to_json_value(entry.payload),
        "meta": to_json_value(entry.meta),
        "date": entry.date,
    }


def tape_entry_from_dict(value: dict[str, Any]) -> TapeEntry:
    return TapeEntry(
        id=int(value.get("id", 0)),
        kind=str(value.get("kind", "event")),
        payload=dict(value.get("payload") or {}),
        meta=dict(value.get("meta") or {}),
        date=str(value.get("date", "")) or utc_now(),
    )
