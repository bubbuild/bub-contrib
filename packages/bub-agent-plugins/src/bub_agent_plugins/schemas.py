"""Load the canonical Agent Plugins schemas bundled with this package."""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any


def _load(name: str) -> dict[str, Any]:
    schema = json.loads(
        files("bub_agent_plugins").joinpath("schema", name).read_text(encoding="utf-8")
    )
    if not isinstance(schema, dict):
        raise RuntimeError(f"bundled Agent Plugins schema is not an object: {name}")
    return schema


PLUGIN_SCHEMA = _load("plugin.schema.json")
MCP_SCHEMA = _load("mcp.schema.json")
PLUGIN_SCHEMA_ID = str(PLUGIN_SCHEMA["$id"])
MCP_SCHEMA_ID = str(MCP_SCHEMA["$id"])
MCP_SERVER_SCHEMA: dict[str, Any] = {
    "$schema": MCP_SCHEMA["$schema"],
    "$ref": "#/$defs/server",
    "$defs": MCP_SCHEMA["$defs"],
}
