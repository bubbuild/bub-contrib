from __future__ import annotations

from typing import TYPE_CHECKING, cast

from bub import tool
from republic.tape.query import TapeQuery
from republic.tape.store import AsyncTapeStore
from republic.tools.context import ToolContext

from bub_tape_explore.explore import build_explore
from bub_tape_explore.map import build_nodes, find_node
from bub_tape_explore.presenters import render_explore, render_map, render_window
from bub_tape_explore.window import select_window_entries

if TYPE_CHECKING:
    from bub.builtin.agent import Agent


def _get_agent(context: ToolContext) -> Agent:
    if "_runtime_agent" not in context.state:
        raise RuntimeError("no runtime agent found in tool context")
    return cast("Agent", context.state["_runtime_agent"])


async def _entries_for_current_tape(context: ToolContext) -> tuple[str, list]:
    tape_name = context.tape or ""
    if not tape_name:
        raise RuntimeError("no current tape")
    agent = _get_agent(context)
    query = TapeQuery[AsyncTapeStore](tape=tape_name, store=agent.tapes._store)
    entries = list(await agent.tapes.search(query))
    return tape_name, entries


def _parse_node_ids(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@tool(context=True, name="tape.map")
async def tape_map(limit: int = 8, *, context: ToolContext) -> str:
    """Compress the current tape into a bounded list of structural nodes."""
    tape_name, entries = await _entries_for_current_tape(context)
    nodes = build_nodes(entries, limit=limit)
    return render_map(tape_name, nodes)


@tool(context=True, name="tape.explore")
async def tape_explore(
    limit: int = 3,
    node_ids: str = "",
    before_messages: int = 1,
    after_items: int = 3,
    *,
    context: ToolContext,
) -> str:
    """Show before/after/stats summaries for a bounded list of tape nodes."""
    tape_name, entries = await _entries_for_current_tape(context)
    nodes = build_nodes(entries)
    items = build_explore(
        entries,
        nodes,
        limit=limit,
        node_ids=_parse_node_ids(node_ids),
        before_messages=before_messages,
        after_items=after_items,
    )
    return render_explore(tape_name, items)


@tool(context=True, name="tape.window")
async def tape_window(
    node_id: str,
    limit: int | None = None,
    filter: str = "",
    *,
    context: ToolContext,
) -> str:
    """Render the raw tape entries for one structural node."""
    tape_name, entries = await _entries_for_current_tape(context)
    nodes = build_nodes(entries)
    node = find_node(nodes, node_id)
    if node is None:
        return f"error: node '{node_id}' was not found in tape '{tape_name}'"
    window_entries = select_window_entries(entries, node, limit=limit, filter_text=filter)
    return render_window(node, window_entries, filter_text=filter)
