from __future__ import annotations

import json


def shorten_text(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def preview_message_content(content: object, *, limit: int) -> str:
    if not isinstance(content, str):
        return ""
    extracted = _extract_nested_message(content)
    return shorten_text(extracted, limit)


def is_generic_runtime_message(content: str) -> bool:
    normalized = " ".join(content.split())
    return normalized.startswith("Continue the task.")


def _extract_nested_message(content: str) -> str:
    marker = "\n---\n"
    if marker not in content:
        return content
    _, _, tail = content.partition(marker)
    try:
        payload = json.loads(tail)
    except json.JSONDecodeError:
        return content
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message
    return content
