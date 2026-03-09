from __future__ import annotations

from republic.tape.entries import TapeEntry

from bub_tape_explore.explore import ExploreNode
from bub_tape_explore.map import TapeNode
from bub_tape_explore.rendering import render_entry


def render_map(tape_name: str, nodes: list[TapeNode]) -> str:
    lines = [f"tape: {tape_name}", "nodes:"]
    if not nodes:
        lines.append("  - (none)")
        return "\n".join(lines)
    for node in nodes:
        lines.extend(
            [
                f"  - node_id: {node.node_id}",
                f"    kind: {node.kind}",
                f"    start_entry_id: {node.start_entry_id}",
                f"    end_entry_id: {node.end_entry_id}",
                f"    label: {node.label}",
                f"    status: {node.status}",
            ]
        )
    return "\n".join(lines)


def render_explore(tape_name: str, items: list[ExploreNode]) -> str:
    lines = [f"tape: {tape_name}", "nodes:"]
    if not items:
        lines.append("  - (none)")
        return "\n".join(lines)
    for item in items:
        lines.extend(
            [
                f"  - node_id: {item.node.node_id}",
                f"    kind: {item.node.kind}",
                f"    start_entry_id: {item.node.start_entry_id}",
                f"    end_entry_id: {item.node.end_entry_id}",
                "    before:",
            ]
        )
        lines.extend(_render_list("      - ", item.before))
        lines.append("    after:")
        lines.extend(_render_list("      - ", item.after))
        lines.append("    stats:")
        for key, value in item.stats:
            lines.append(f"      - {key}: {value}")
    return "\n".join(lines)


def render_window(
    node: TapeNode,
    entries: list[TapeEntry],
    *,
    filter_text: str = "",
) -> str:
    lines = [
        f"node_id: {node.node_id}",
        f"kind: {node.kind}",
        f"start_entry_id: {node.start_entry_id}",
        f"end_entry_id: {node.end_entry_id}",
        f"filter: {filter_text or '(none)'}",
        "entries:",
    ]
    if not entries:
        lines.append("  - (none)")
        return "\n".join(lines)
    for entry in entries:
        lines.append(f"  - [{entry.id}][{entry.kind}] {render_entry(entry, limit=80)}")
    return "\n".join(lines)


def _render_list(prefix: str, items: list[str]) -> list[str]:
    if not items:
        return [f"{prefix}(none)"]
    return [f"{prefix}{item}" for item in items]
