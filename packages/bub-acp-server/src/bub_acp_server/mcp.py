"""Adapt ACP session MCP configuration to bub-mcp's connection lifecycle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acp.exceptions import RequestError
from acp.schema import HttpMcpServer, McpServerStdio, SseMcpServer
from bub_mcp.plugin import MCPChannel

type ACPMcpServer = HttpMcpServer | SseMcpServer | McpServerStdio


def server_configs(
    servers: list[ACPMcpServer] | None, cwd: Path
) -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}
    for server in servers or []:
        name = server.name.strip()
        if not name or name in configs:
            raise RequestError.invalid_params(
                {
                    "field": "mcpServers",
                    "reason": "Server names must be nonempty and unique",
                }
            )
        if isinstance(server, McpServerStdio):
            configs[name] = {
                "transport": "stdio",
                "command": server.command,
                "args": list(server.args),
                "env": {item.name: item.value for item in server.env},
                "cwd": str(cwd),
            }
        elif isinstance(server, HttpMcpServer | SseMcpServer):
            configs[name] = {
                "transport": "http" if isinstance(server, HttpMcpServer) else "sse",
                "url": server.url,
                "headers": {item.name: item.value for item in server.headers},
            }
        else:
            raise RequestError.invalid_params(
                {"field": "mcpServers", "reason": "Unsupported MCP transport"}
            )
    return configs


async def connect_session_mcp(
    configs: dict[str, dict[str, Any]],
) -> MCPChannel | None:
    if not configs:
        return None
    channel = MCPChannel.from_server_configs(configs)
    try:
        await channel.connect()
        states = channel.list()
        failed = [
            name for name in configs if name not in states or not states[name].connected
        ]
        if failed:
            # Connection exceptions can contain URLs, headers or environment values.
            raise RequestError.internal_error(
                {"reason": "Failed to connect MCP servers", "servers": failed}
            )
        return channel
    except BaseException:
        await channel.stop()
        raise
