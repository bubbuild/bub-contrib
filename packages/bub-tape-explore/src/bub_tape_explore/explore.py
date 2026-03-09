from __future__ import annotations

from dataclasses import dataclass

from republic.tape.entries import TapeEntry

from bub_tape_explore.map import TapeNode, entries_for_node, find_node
from bub_tape_explore.rendering import event_name, event_status, render_message, render_tool_call, render_tool_result


@dataclass(frozen=True)
class ExploreNode:
    node: TapeNode
    before: list[str]
    after: list[str]
    stats: list[tuple[str, str]]


def build_explore(
    entries: list[TapeEntry],
    nodes: list[TapeNode],
    *,
    limit: int,
    node_ids: list[str] | None = None,
    before_messages: int = 1,
    after_items: int = 3,
) -> list[ExploreNode]:
    selected: list[TapeNode] = []
    if node_ids:
        for node_id in node_ids:
            node = find_node(nodes, node_id)
            if node is not None:
                selected.append(node)
    else:
        selected = nodes[:limit]

    result: list[ExploreNode] = []
    for node in selected:
        node_entries = entries_for_node(entries, node)
        result.append(
            ExploreNode(
                node=node,
                before=_before_messages(entries, node, before_messages),
                after=_after_items(node_entries, after_items),
                stats=_stats(node_entries, node),
            )
        )
    return result
def _before_messages(entries: list[TapeEntry], node: TapeNode, limit: int) -> list[str]:
    messages = [
        _render_message(entry)
        for entry in entries
        if entry.id < node.start_entry_id and entry.kind == "message"
    ]
    filtered = [message for message in messages if message]
    if limit <= 0:
        return filtered
    return filtered[-limit:]


def _after_items(entries: list[TapeEntry], limit: int) -> list[str]:
    items: list[str] = []
    for entry in entries:
        rendered = _render_after_entry(entry)
        if rendered:
            items.append(rendered)
    if limit <= 0:
        return items
    return items[-limit:]


def _stats(entries: list[TapeEntry], node: TapeNode) -> list[tuple[str, str]]:
    messages = sum(1 for entry in entries if entry.kind == "message")
    tool_calls = sum(1 for entry in entries if entry.kind == "tool_call")
    tool_results = sum(1 for entry in entries if entry.kind == "tool_result")
    events = sum(1 for entry in entries if entry.kind == "event")
    anchors = sum(1 for entry in entries if entry.kind == "anchor")
    total_entries = len(entries)
    return [
        ("entries", str(total_entries)),
        ("messages", str(messages)),
        ("tool_calls", str(tool_calls)),
        ("tool_results", str(tool_results)),
        ("events", str(events)),
        ("anchors", str(anchors)),
        ("status", node.status),
    ]


def _render_after_entry(entry: TapeEntry) -> str:
    if entry.kind == "system":
        return ""
    if entry.kind == "message":
        return render_message(entry, limit=64)
    if entry.kind == "tool_call":
        return render_tool_call(entry)
    if entry.kind == "tool_result":
        return render_tool_result(entry, limit=64)
    if entry.kind == "anchor":
        name = entry.payload.get("name")
        return f"anchor: {name}" if isinstance(name, str) else "anchor"
    if entry.kind == "event":
        name = event_name(entry)
        if not name:
            return ""
        if name == "run":
            return ""
        status = event_status(entry)
        if status:
            return f"{name}: {status}"
        return name
    return ""


def _render_message(entry: TapeEntry) -> str:
    return render_message(entry, limit=64)
