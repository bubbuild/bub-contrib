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
    tools = [tool for tool in tools if tool is not mcp_describe]
    available = {
        tool.name: tool
        for tool in tools
        if isinstance(tool, MCPTool) and tool.agent_use
    }
    if not available:
        tape.context.state.pop(MCP_TOOLS_STATE_KEY, None)
        return tools, ""
    # The current allowed tool list, not recorded names, defines the lookup scope.
    tape.context.state[MCP_TOOLS_STATE_KEY] = available
    loaded = await loaded_tool_names(tape)
    pending = {name: tool for name, tool in available.items() if name not in loaded}
    native = [tool for tool in tools if tool.name not in pending]
    return native + [mcp_describe], render_tools_prompt(pending.values())
