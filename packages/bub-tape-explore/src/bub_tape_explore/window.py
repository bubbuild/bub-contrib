from __future__ import annotations

from republic.tape.entries import TapeEntry

from bub_tape_explore.map import TapeNode, entries_for_node
from bub_tape_explore.rendering import event_name, render_entry, role_name


def select_window_entries(
    entries: list[TapeEntry],
    node: TapeNode,
    *,
    limit: int | None = None,
    filter_text: str = "",
) -> list[TapeEntry]:
    node_entries = entries_for_node(entries, node)
    if filter_text.strip():
        node_entries = [entry for entry in node_entries if _matches_filter(entry, filter_text)]
    if limit is not None and limit >= 0:
        return node_entries[:limit]
    return node_entries


def _matches_filter(entry: TapeEntry, filter_text: str) -> bool:
    needles = [item.strip().casefold() for item in filter_text.split(",") if item.strip()]
    if not needles:
        return True
    structured_values = {
        entry.kind.casefold(),
        event_name(entry).casefold() if event_name(entry) else "",
        role_name(entry).casefold() if role_name(entry) else "",
    }
    rendered = render_entry(entry, limit=80).casefold()
    for needle in needles:
        if needle in structured_values:
            return True
        if any(value.startswith(f"{needle}_") for value in structured_values if value):
            return True
        if needle not in _KNOWN_FILTER_TERMS and needle in rendered:
            return True
    return False


_KNOWN_FILTER_TERMS = {
    "anchor",
    "event",
    "fork",
    "merge",
    "message",
    "run",
    "system",
    "tool",
    "tool_call",
    "tool_result",
    "user",
    "assistant",
}
