from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from bub.envelope import normalize_envelope
from bub.streaming import StreamEvent
from bub.tape import TapeEntry, utc_now

BUB_EXTISM_ABI_VERSION = "bub.extism.v1"
_SKIP_JSON_VALUE = object()


class ExtismHookError(RuntimeError):
    pass


class ExtismHookSkip(Exception):
    pass


def build_request(hook_name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "abi_version": BUB_EXTISM_ABI_VERSION,
        "hook": hook_name,
        "args": mapping_to_json(args),
    }


def decode_response(raw_result: Any) -> Any:
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
        raise ExtismHookError(_error_message(error))
    if "value" in parsed:
        return parsed["value"]
    if "text" in parsed:
        return parsed["text"]
    return parsed


def result_to_text(raw_result: Any) -> str:
    if isinstance(raw_result, str):
        return raw_result
    if isinstance(raw_result, bytes | bytearray | memoryview):
        return bytes(raw_result).decode("utf-8")
    return bytes(raw_result).decode("utf-8")


def message_to_json(message: Any) -> dict[str, Any]:
    return mapping_to_json(normalize_envelope(message))


def error_to_json(error: Exception) -> dict[str, str]:
    return {
        "type": type(error).__name__,
        "message": str(error),
    }


def mapping_to_json(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): encoded
        for key, value in mapping.items()
        if (encoded := _encode_or_skip(value)) is not _SKIP_JSON_VALUE
    }


def state_to_json(state: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): encoded
        for key, value in state.items()
        if not str(key).startswith("_runtime_")
        and (encoded := _encode_or_skip(value)) is not _SKIP_JSON_VALUE
    }


def tape_entry_to_dict(entry: TapeEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "payload": mapping_to_json(entry.payload),
        "meta": mapping_to_json(entry.meta),
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


def _error_message(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("message", "Extism hook returned an error"))
    return str(error)


def _encode_or_skip(value: Any) -> Any:
    try:
        return _encode_json_value(value)
    except (TypeError, ValueError, RecursionError):
        return _SKIP_JSON_VALUE


def _encode_json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return mapping_to_json({str(key): item for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray | memoryview
    ):
        return [
            encoded
            for item in value
            if (encoded := _encode_or_skip(item)) is not _SKIP_JSON_VALUE
        ]
    if isinstance(value, StreamEvent):
        return {
            "kind": value.kind,
            "data": mapping_to_json(value.data),
        }
    if isinstance(value, TapeEntry):
        return tape_entry_to_dict(value)
    if is_dataclass(value):
        dataclass_value = asdict(value)
        if not isinstance(dataclass_value, Mapping):
            raise TypeError("Dataclass value must encode to a mapping")
        return mapping_to_json(dataclass_value)
    if hasattr(value, "__dict__"):
        return message_to_json(value)
    raise TypeError(f"Unsupported Extism JSON value: {type(value).__name__}")
