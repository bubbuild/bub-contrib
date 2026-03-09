from __future__ import annotations

from republic.tape.entries import TapeEntry

from bub_tape_explore.text import preview_message_content, shorten_text


def event_name(entry: TapeEntry) -> str:
    name = entry.payload.get("name")
    return name if isinstance(name, str) else ""


def event_status(entry: TapeEntry) -> str:
    if event_name(entry) == "loop.step":
        payload = entry.meta.get("payload")
        if isinstance(payload, dict):
            status = payload.get("status")
            if isinstance(status, str):
                return status
    if event_name(entry) == "run":
        data = entry.payload.get("data")
        if isinstance(data, dict):
            status = data.get("status")
            if isinstance(status, str):
                return status
    return ""


def role_name(entry: TapeEntry) -> str:
    role = entry.payload.get("role")
    return role if isinstance(role, str) else ""


def render_message(entry: TapeEntry, *, limit: int) -> str:
    role = role_name(entry)
    content = entry.payload.get("content")
    if not role or not isinstance(content, str):
        return ""
    return f"{role}: {preview_message_content(content, limit=limit)}"


def render_tool_call(entry: TapeEntry) -> str:
    calls = entry.payload.get("calls")
    if not isinstance(calls, list) or not calls:
        return "tool_call"
    names = [name for call in calls if (name := _tool_name(call))]
    suffix = ", ".join(names) if names else "tool_call"
    return f"tool_call: {suffix}"


def render_tool_result(entry: TapeEntry, *, limit: int) -> str:
    results = entry.payload.get("results")
    if not isinstance(results, list) or not results:
        return "tool_result"
    return f"tool_result: {shorten_text(_stringify(results[0]), limit)}"


def render_event(entry: TapeEntry) -> str:
    name = event_name(entry)
    if not name:
        return ""
    status = event_status(entry)
    if status:
        return f"{name} status={status}"
    return name


def render_entry(entry: TapeEntry, *, limit: int) -> str:
    if entry.kind == "message":
        return render_message(entry, limit=limit)
    if entry.kind == "system":
        content = entry.payload.get("content")
        if isinstance(content, str):
            return shorten_text(content, limit)
        return ""
    if entry.kind == "tool_call":
        return render_tool_call(entry)
    if entry.kind == "tool_result":
        return render_tool_result(entry, limit=limit)
    if entry.kind == "anchor":
        name = entry.payload.get("name")
        if isinstance(name, str):
            return f"anchor: {name}"
        return "anchor"
    if entry.kind == "event":
        return render_event(entry)
    return shorten_text(str(entry.payload), limit)


def _tool_name(call: object) -> str:
    if not isinstance(call, dict):
        return ""
    function = call.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    return name if isinstance(name, str) else ""


def _stringify(value: object) -> str:
    if isinstance(value, str):
        return value
    return str(value)
