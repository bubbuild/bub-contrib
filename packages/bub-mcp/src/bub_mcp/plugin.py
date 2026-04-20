from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

import fastmcp
import mcp.types
import typer
from bub import hookimpl, tool
from bub.channels import Channel
from bub.tools import REGISTRY
from bub.types import Envelope, MessageHandler, State
from loguru import logger
from republic import Tool, ToolContext

from bub_mcp.config import MCPSettings

TOOL_PREFIX = "mcp."
LIFECYCLE_CHANNEL_NAME = "mcp.lifecycle"


def _create_fastmcp_client(
    config: dict[str, Any], *, init_timeout_seconds: float | None
) -> fastmcp.Client[Any]:

    kwargs: dict[str, Any] = {}
    if init_timeout_seconds is not None:
        kwargs["init_timeout"] = init_timeout_seconds
    return fastmcp.Client(config, **kwargs)


def _tool_name(server_name: str, remote_name: str) -> str:
    normalized_name = remote_name
    if not remote_name.startswith(f"{server_name}_"):
        normalized_name = f"{server_name}_{remote_name}"
    return f"{TOOL_PREFIX}{normalized_name}"


def _tool_parameters(remote_tool: mcp.types.Tool) -> dict[str, Any]:
    schema = remote_tool.inputSchema
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return {"type": "object", "properties": {}}


def _render_binary_placeholder(kind: str, item: Any) -> str:
    mime_type = getattr(item, "mimeType", "application/octet-stream")
    return f"[Binary content: {kind} {mime_type}]"


def _format_tool_result(result: Any) -> str:
    content = getattr(result, "content", []) or []
    blocks: list[str] = []

    for item in content:
        item_type = getattr(item, "type", None)
        if item_type == "text":
            text = getattr(item, "text", "")
            if isinstance(text, str) and text:
                blocks.append(text)
            continue
        if item_type == "resource":
            resource = getattr(item, "resource", None)
            text = getattr(resource, "text", None)
            if isinstance(text, str) and text:
                blocks.append(text)
                continue
            uri = getattr(resource, "uri", "unknown")
            mime_type = getattr(resource, "mimeType", "application/octet-stream")
            blocks.append(f"[Resource content: {uri} ({mime_type})]")
            continue
        if item_type == "image":
            blocks.append(_render_binary_placeholder("image", item))
            continue
        if item_type == "audio":
            blocks.append(_render_binary_placeholder("audio", item))
            continue

    structured = getattr(result, "structuredContent", None)
    if not blocks and structured is not None:
        return json.dumps(structured, ensure_ascii=False, indent=2, sort_keys=True)

    rendered = "\n".join(blocks).strip()
    if rendered:
        return rendered

    is_error = bool(getattr(result, "isError", False))
    return "error: remote MCP tool returned no content" if is_error else "ok"


@dataclass
class MCPServerState:
    client: Any | None = None
    tools: list[Tool] = field(default_factory=list)
    connected: bool = False
    error: str | None = None


class MCPChannel(Channel):
    name = LIFECYCLE_CHANNEL_NAME

    def __init__(self) -> None:
        self.settings = MCPSettings()
        self._lock = asyncio.Lock()
        self._bootstrap_task: asyncio.Task[None] | None = None
        self._servers: dict[str, MCPServerState] = {}
        self._stop_event: asyncio.Event | None = None

    async def start(self, stop_event: asyncio.Event) -> None:
        self._stop_event = stop_event
        if any(server.connected for server in self._servers.values()):
            return
        if self._bootstrap_task is not None and not self._bootstrap_task.done():
            return
        self._bootstrap_task = asyncio.create_task(
            self._bootstrap(stop_event), name="bub-mcp.bootstrap"
        )

    async def stop(self) -> None:
        task = self._bootstrap_task
        self._bootstrap_task = None
        self._stop_event = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        async with self._lock:
            clients = [
                server.client
                for server in self._servers.values()
                if server.client is not None
            ]
            for server in self._servers.values():
                server.client = None
                server.connected = False

        for client in clients:
            await self._close_client(client)

    def list(self) -> dict[str, MCPServerState]:
        return self._servers.copy()

    async def add(self, name: str, server: dict[str, Any]) -> dict[str, dict[str, Any]]:
        server_name = name.strip()
        if not server_name:
            raise ValueError("server name must not be blank")
        if not isinstance(server, dict) or not server:
            raise ValueError("server config must be a non-empty mapping")

        mcp_servers = self.settings.read_mcp_servers()

        if server_name in mcp_servers:
            raise ValueError(f"MCP server '{server_name}' already exists")
        mcp_servers[server_name] = server
        self.settings.write_mcp_servers(mcp_servers)
        return mcp_servers

    async def remove(self, name: str) -> dict[str, dict[str, Any]]:
        server_name = name.strip()
        if not server_name:
            raise ValueError("server name must not be blank")

        mcp_servers = self.settings.read_mcp_servers()

        if server_name not in mcp_servers:
            raise KeyError(server_name)
        mcp_servers.pop(server_name)
        self.settings.write_mcp_servers(mcp_servers)
        return mcp_servers

    async def call_tool(
        self, server_name: str, remote_name: str, arguments: dict[str, Any]
    ) -> str:
        server = self._servers.get(server_name)
        if server is None or server.client is None:
            raise RuntimeError(
                f"MCP client for server '{server_name}' is not connected"
            )
        result = await server.client.call_tool(remote_name, arguments or {})
        return _format_tool_result(result)

    def bootstrap(self) -> None:
        async def main() -> None:
            stop_event = asyncio.Event()
            await self._bootstrap(stop_event)

        asyncio.run(main())

    async def _bootstrap(self, stop_event: asyncio.Event) -> None:
        async with self._lock:
            try:
                config = self.settings.read_mcp_servers()
                if not config:
                    self._servers = {}
                    return

                config_items = list(config.items())
                server_states = await asyncio.gather(
                    *[
                        self._connect_server(server_name, server_config)
                        for server_name, server_config in config_items
                    ]
                )

                self._servers = {
                    server_name: server_state
                    for (server_name, _server_config), server_state in zip(
                        config_items, server_states, strict=False
                    )
                }

                for server_name, server in self._servers.items():
                    for tool in server.tools:
                        REGISTRY[tool.name] = tool

                if self._servers and not any(
                    server.connected for server in self._servers.values()
                ):
                    stop_event.set()
            except asyncio.CancelledError:
                for server in self._servers.values():
                    if server.client is not None:
                        await self._close_client(server.client)
                        server.client = None
                    server.connected = False
                raise
            except Exception as exc:
                for server in self._servers.values():
                    if server.client is not None:
                        await self._close_client(server.client)
                        server.client = None
                    server.connected = False
                logger.warning("bub-mcp bootstrap failed: {}", exc)
                stop_event.set()

    async def _connect_server(
        self, server_name: str, server_config: dict[str, Any]
    ) -> MCPServerState:
        server = MCPServerState()
        client: Any | None = None
        try:
            client = _create_fastmcp_client(
                {server_name: server_config},
                init_timeout_seconds=self.settings.init_timeout_seconds,
            )
            await client.__aenter__()
            remote_tools = await client.list_tools()
            server.client = client
            server.connected = True
            server.error = None

            for remote_tool in remote_tools:
                tool = self._build_tool(server_name, remote_tool)
                if tool is not None:
                    server.tools.append(tool)
        except asyncio.CancelledError:
            if client is not None:
                await self._close_client(client)
            raise
        except Exception as exc:
            if client is not None:
                await self._close_client(client)
            self._record_failed_server(server_name, server, exc)
        return server

    def _build_tool(self, server_name: str, remote_tool: mcp.types.Tool) -> Tool | None:
        remote_name = remote_tool.name.strip()
        if not remote_name:
            return None
        bub_name = _tool_name(server_name, remote_name)
        return Tool(
            name=bub_name,
            description=str(remote_tool.description or f"MCP tool {remote_name}"),
            parameters=_tool_parameters(remote_tool),
            handler=self._make_handler(server_name, remote_name),
        )

    def _record_failed_server(
        self, server_name: str, server: MCPServerState, exc: Exception
    ) -> None:
        error_message = str(exc) or exc.__class__.__name__
        server.client = None
        server.connected = False
        server.error = error_message
        logger.warning(
            "bub-mcp failed to connect MCP server '{}': {}", server_name, error_message
        )

    def _make_handler(self, server_name: str, remote_name: str):
        async def _handler(**payload: Any) -> str:
            return await self.call_tool(server_name, remote_name, payload)

        return _handler

    @staticmethod
    async def _close_client(client: Any) -> None:
        with contextlib.suppress(Exception):
            await client.__aexit__(None, None, None)


class MCPPlugin:
    def __init__(self, framework: Any) -> None:
        del framework
        self._manager = MCPChannel()

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> State:
        return {"mcp": self._manager}

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        del message_handler
        return [self._manager]

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        from bub_mcp.cli import make_mcp_command

        app.add_typer(
            make_mcp_command(self._manager), name="mcp", help="Manage MCP servers"
        )


@tool(name="mcp", context=True)
def mcp_list(*, context: ToolContext) -> str:
    """List configured MCP servers."""
    manager = context.state.get("mcp")
    if not isinstance(manager, MCPChannel):
        raise RuntimeError("MCP channel is not available in state")
    servers = manager.list()
    if not servers:
        return "No MCP servers configured."
    lines: list[str] = []
    lines.append("🔌 MCP Servers:")
    for name, server in servers.items():
        lines.append(f"- {name}")
        if server.connected:
            lines.append("  Status: Connected")
            lines.append(
                f"  Tools: {', '.join(tool.name for tool in server.tools) if server.tools else 'No tools'}"
            )
        else:
            lines.append("  Status: Not connected")
            if server.error:
                lines.append(f"  Error: {server.error}")
    return "\n".join(lines)
