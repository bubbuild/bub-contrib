"""Bundled Agent Plugins 1.0.0 schemas used without runtime network access."""

from __future__ import annotations

from typing import Any

PLUGIN_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_ID = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

PLUGIN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": PLUGIN_SCHEMA_ID,
    "type": "object",
    "properties": {
        "$schema": {"const": PLUGIN_SCHEMA_ID},
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "pattern": r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$",
        },
        "version": {"type": "string"},
        "description": {"type": "string"},
        "author": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "url": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "homepage": {"type": "string"},
        "repository": {"type": "string"},
        "license": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "extensions": {
            "type": "object",
            "additionalProperties": {"type": "object"},
        },
    },
    "required": ["$schema", "name"],
    "additionalProperties": False,
}

MCP_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": MCP_SCHEMA_ID,
    "type": "object",
    "properties": {
        "$schema": {"const": MCP_SCHEMA_ID},
        "mcpServers": {
            "type": "object",
            "additionalProperties": {"$ref": "#/$defs/server"},
        },
    },
    "required": ["$schema", "mcpServers"],
    "additionalProperties": False,
    "$defs": {
        "server": {
            "oneOf": [
                {"$ref": "#/$defs/stdioServer"},
                {"$ref": "#/$defs/streamableHttpServer"},
                {"$ref": "#/$defs/sseServer"},
            ]
        },
        "stdioServer": {
            "type": "object",
            "properties": {
                "type": {"const": "stdio"},
                "command": {"type": "string", "minLength": 1},
                "args": {"type": "array", "items": {"type": "string"}},
                "env": {
                    "type": "object",
                    "propertyNames": {"not": {"enum": ["PLUGIN_ROOT", "PLUGIN_DATA"]}},
                    "additionalProperties": {"type": "string"},
                },
                "cwd": {
                    "type": "string",
                    "pattern": r"^(?:\./|\$\{PLUGIN_ROOT\}(?:/|$)|\$\{PLUGIN_DATA\}(?:/|$))",
                },
            },
            "required": ["type", "command"],
            "additionalProperties": False,
        },
        "streamableHttpServer": {
            "type": "object",
            "properties": {
                "type": {"const": "streamable-http"},
                "url": {"type": "string", "minLength": 1},
                "headers": {"$ref": "#/$defs/headers"},
            },
            "required": ["type", "url"],
            "additionalProperties": False,
        },
        "sseServer": {
            "type": "object",
            "properties": {
                "type": {"const": "sse"},
                "url": {"type": "string", "minLength": 1},
                "headers": {"$ref": "#/$defs/headers"},
            },
            "required": ["type", "url"],
            "additionalProperties": False,
        },
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    },
}

MCP_SERVER_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$ref": "#/$defs/server",
    "$defs": MCP_SCHEMA["$defs"],
}
