from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from republic.tape.entries import TapeEntry

from bub_tape_explore.rendering import event_name, event_status, render_message
from bub_tape_explore.text import is_generic_runtime_message, shorten_text

NodeKind = Literal["anchor", "run", "fork", "merge"]


@dataclass(frozen=True)
class TapeNode:
    node_id: str
    kind: NodeKind
    start_entry_id: int
    end_entry_id: int
    label: str
    status: str


def build_nodes(entries: list[TapeEntry], *, limit: int | None = None) -> list[TapeNode]:
    nodes: list[TapeNode] = []
    index = 0
    total = len(entries)
    while index < total:
        entry = entries[index]
        if entry.kind == "anchor":
            nodes.append(_build_anchor_node(entry))
            index += 1
            continue
        marker_kind = _marker_kind(entry)
        if marker_kind is not None:
            nodes.append(_build_marker_node(entry, marker_kind))
            index += 1
            continue
        if _is_loop_step_start(entry):
            end_index = _find_step_end(entries, index)
            nodes.append(_build_run_node(entries[index : end_index + 1]))
            index = end_index + 1
            continue
        run_id = _run_id(entry)
        if run_id is not None:
            end_index = _find_run_cluster_end(entries, index, run_id)
            nodes.append(_build_run_node(entries[index : end_index + 1]))
            index = end_index + 1
            continue
        loose_end_index = _find_loose_end(entries, index)
        nodes.append(_build_run_node(entries[index : loose_end_index + 1]))
        index = loose_end_index + 1

    if limit is not None and limit > 0:
        nodes = nodes[-limit:]
    return list(reversed(nodes))


def find_node(nodes: list[TapeNode], node_id: str) -> TapeNode | None:
    for node in nodes:
        if node.node_id == node_id:
            return node
    return None


def entries_for_node(entries: list[TapeEntry], node: TapeNode) -> list[TapeEntry]:
    return [entry for entry in entries if node.start_entry_id <= entry.id <= node.end_entry_id]


def _build_anchor_node(entry: TapeEntry) -> TapeNode:
    name = str(entry.payload.get("name", f"anchor-{entry.id}"))
    return TapeNode(
        node_id=f"anchor:{name}:{entry.id}",
        kind="anchor",
        start_entry_id=entry.id,
        end_entry_id=entry.id,
        label=name,
        status="anchor",
    )


def _build_marker_node(entry: TapeEntry, kind: NodeKind) -> TapeNode:
    label = _event_name(entry) or kind
    return TapeNode(
        node_id=f"{kind}:{entry.id}",
        kind=kind,
        start_entry_id=entry.id,
        end_entry_id=entry.id,
        label=label,
        status=kind,
    )


def _build_run_node(group: list[TapeEntry]) -> TapeNode:
    first = group[0]
    last = group[-1]
    run_id = _first_run_id(group)
    node_id = f"run:{run_id}" if run_id is not None else f"run:{first.id}-{last.id}"
    return TapeNode(
        node_id=node_id,
        kind="run",
        start_entry_id=first.id,
        end_entry_id=last.id,
        label=_label_for_group(group),
        status=_status_for_group(group),
    )


def _find_step_end(entries: list[TapeEntry], start_index: int) -> int:
    index = start_index + 1
    total = len(entries)
    while index < total:
        entry = entries[index]
        if entry.kind == "anchor" or _marker_kind(entry) is not None or _is_loop_step_start(entry):
            return index - 1
        if _is_loop_step(entry):
            return index
        index += 1
    return total - 1


def _find_run_cluster_end(entries: list[TapeEntry], start_index: int, run_id: str) -> int:
    index = start_index + 1
    end_index = start_index
    total = len(entries)
    while index < total:
        entry = entries[index]
        if entry.kind == "anchor" or _marker_kind(entry) is not None or _is_loop_step_start(entry):
            break
        entry_run_id = _run_id(entry)
        if entry_run_id == run_id:
            end_index = index
            index += 1
            continue
        if _is_loop_step(entry):
            return index
        break
    return end_index


def _find_loose_end(entries: list[TapeEntry], start_index: int) -> int:
    index = start_index + 1
    total = len(entries)
    while index < total:
        entry = entries[index]
        if entry.kind == "anchor" or _marker_kind(entry) is not None or _is_loop_step_start(entry) or _run_id(entry):
            break
        if _is_loop_step(entry):
            return index
        index += 1
    return index - 1


def _is_loop_step_start(entry: TapeEntry) -> bool:
    return entry.kind == "event" and event_name(entry) == "loop.step.start"


def _is_loop_step(entry: TapeEntry) -> bool:
    return entry.kind == "event" and event_name(entry) == "loop.step"


def _marker_kind(entry: TapeEntry) -> NodeKind | None:
    if entry.kind != "event":
        return None
    name = event_name(entry)
    if not name:
        return None
    if name == "fork" or name.startswith("fork."):
        return "fork"
    if name == "merge" or name.startswith("merge."):
        return "merge"
    return None


def _run_id(entry: TapeEntry) -> str | None:
    run_id = entry.meta.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    return None


def _first_run_id(entries: list[TapeEntry]) -> str | None:
    for entry in entries:
        run_id = _run_id(entry)
        if run_id is not None:
            return run_id
    return None


def _status_for_group(entries: list[TapeEntry]) -> str:
    for entry in reversed(entries):
        loop_status = _loop_status(entry)
        if loop_status is not None:
            return loop_status
        run_status = _run_status(entry)
        if run_status is not None:
            return run_status
    return "unknown"


def _loop_status(entry: TapeEntry) -> str | None:
    if not _is_loop_step(entry):
        return None
    payload = entry.meta.get("payload")
    if isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status:
            return status
    return None


def _run_status(entry: TapeEntry) -> str | None:
    if entry.kind != "event" or event_name(entry) != "run":
        return None
    return event_status(entry) or None


def _label_for_group(entries: list[TapeEntry]) -> str:
    for entry in entries:
        if entry.kind == "message":
            preview = render_message(entry, limit=48)
            if preview:
                if is_generic_runtime_message(preview.partition(": ")[2]):
                    continue
                return preview
        if entry.kind == "tool_call":
            calls = entry.payload.get("calls")
            if isinstance(calls, list) and calls:
                rendered = _render_tool_call(calls[0])
                if rendered:
                    return rendered
    for entry in entries:
        if entry.kind == "message":
            preview = render_message(entry, limit=48)
            if preview:
                return preview
    first = entries[0]
    return f"entries {first.id}-{entries[-1].id}"


def _render_tool_call(item: object) -> str:
    if not isinstance(item, dict):
        return ""
    function = item.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return ""
    return f"tool: {name}"


def _shorten(text: str, limit: int = 48) -> str:
    return shorten_text(text, limit)
