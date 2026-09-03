"""Load the canonical Agent Plugins schemas bundled with this package."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
from typing import Any


def _load(name: str) -> dict[str, Any]:
    schema = json.loads(
        files("bub_agent_plugins").joinpath("schema", name).read_text(encoding="utf-8")
    )
    if not isinstance(schema, dict):
        raise RuntimeError(f"bundled Agent Plugins schema is not an object: {name}")
    return schema


@dataclass(frozen=True)
class AgentPluginSchema:
    manifest: dict[str, Any]
    mcp: dict[str, Any]

    @property
    def manifest_id(self) -> str:
        return str(self.manifest["$id"])

    @property
    def mcp_id(self) -> str:
        return str(self.mcp["$id"])

    def mcp_document(self) -> dict[str, Any]:
        """Return a schema that validates the document but not each server."""
        schema = deepcopy(self.mcp)
        schema["properties"]["mcpServers"]["additionalProperties"] = True
        return schema

    def mcp_server(self) -> dict[str, Any]:
        return {
            "$schema": self.mcp["$schema"],
            "$ref": "#/$defs/server",
            "$defs": self.mcp["$defs"],
        }


SCHEMA = AgentPluginSchema(
    manifest=_load("plugin.schema.json"),
    mcp=_load("mcp.schema.json"),
)
SCHEMAS = {SCHEMA.manifest_id: SCHEMA}
PLUGIN_SCHEMA_ID = SCHEMA.manifest_id
MCP_SCHEMA_ID = SCHEMA.mcp_id
PLUGIN_SCHEMA = SCHEMA.manifest
MCP_SCHEMA = SCHEMA.mcp
MCP_SERVER_SCHEMA = SCHEMA.mcp_server()


def find_schema(identifier: object) -> AgentPluginSchema | None:
    return SCHEMAS.get(identifier) if isinstance(identifier, str) else None
