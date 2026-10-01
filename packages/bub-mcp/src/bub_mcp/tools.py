"""MCP-owned discovery and presentation of remote tool definitions."""

from __future__ import annotations

from collections.abc import Iterable

from bub import tool
from bub.tape import Tape
from bub.tools import Tool, ToolContext

MCP_TOOLS_STATE_KEY = "_mcp_tools"
DEFINITIONS_LOADED_EVENT = "mcp.definitions.loaded"


class MCPTool(Tool):
    """A callable supplied by an MCP server."""


async def loaded_tool_names(tape: Tape) -> set[str]:
    query = tape.context.build_query(tape.query()).kinds("event")
    names: set[str] = set()
    for entry in await tape.store.fetch_all(query):
        data = entry.payload.get("data")
        if entry.payload.get("name") != DEFINITIONS_LOADED_EVENT or not isinstance(
            data, dict
        ):
            continue
        recorded = data.get("names")
        if isinstance(recorded, list):
            names.update(name for name in recorded if isinstance(name, str))
    return names


@tool(name="mcp.describe", context=True, preserve=True)
async def mcp_describe(names: list[str], *, context: ToolContext) -> str:
    """Expose complete native definitions for selected MCP tools on the next model call.

    Use exact names from the mcp_tools catalog. Call already available tools directly.
    """
    from bub.builtin.tools import resolve_tool_names

    available: dict[str, Tool] = context.state.get(MCP_TOOLS_STATE_KEY, {})
    resolved = resolve_tool_names(names, all_names=available)
    if not resolved:
        raise ValueError("provide at least one available MCP tool name")
    await context.tape.append_event(
        DEFINITIONS_LOADED_EVENT, {"names": sorted(resolved)}, context=False
    )
    agent = context.state["_runtime_agent"]
    agent.tools.update({name: available[name] for name in resolved})
    aliases = ", ".join(name.replace(".", "_") for name in sorted(resolved))
    return f"Complete native definitions are now available for: {aliases}. Call these tools directly."


def render_tools_prompt(tools: Iterable[Tool]) -> str:
    lines: list[str] = []
    for item in tools:
        description = next(
            (line.strip() for line in item.description.splitlines() if line.strip()), ""
        )
        summary = description.split(". ", 1)[0][:180]
        name = item.name.replace(".", "_")
        lines.append(f"- {name}: {summary}" if summary else f"- {name}")
    if not lines:
        return ""
    return (
        "Call tools whose complete native definitions are already available directly. "
        "The catalog below lists MCP tools without native definitions; use mcp_describe to obtain them by name.\n"
        f"<mcp_tools>\n{'\n'.join(lines)}\n</mcp_tools>"
    )


async def prepare_mcp_tools(tools: list[Tool], tape: Tape) -> tuple[list[Tool], str]:
    agent = tape.context.state["_runtime_agent"]
    scope = tape.context.state.get("_runtime_allowed_tools")
    available = {
        name: item
        for name, item in agent.tool_catalog.items()
        if isinstance(item, MCPTool)
        and item.agent_use
        and (scope is None or name in scope)
    }
    native = [
        item
        for item in tools
        if not isinstance(item, MCPTool) and item is not mcp_describe
    ]
    if not available:
        tape.context.state.pop(MCP_TOOLS_STATE_KEY, None)
        return native, ""
    tape.context.state[MCP_TOOLS_STATE_KEY] = available
    code_mode = tape.context.state.get("code_mode") and any(
        item.name == "run_code" for item in native
    )
    loaded = set(available) if code_mode else await loaded_tool_names(tape)
    selected = {name: item for name, item in available.items() if name in loaded}
    agent.tools.update(selected)
    native.extend(selected.values())
    if code_mode:
        return native, ""
    pending = [item for name, item in available.items() if name not in loaded]
    return native + [mcp_describe], render_tools_prompt(pending)
