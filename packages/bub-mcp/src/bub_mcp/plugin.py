from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Self, TypedDict, final
from weakref import WeakKeyDictionary

import fastmcp
import mcp.types
import typer
import bub
from bub import hookimpl, tool
from bub.channels import Channel, Lifecycle
from bub.tools import Tool, ToolContext
from bub.tape import Tape
from bub.channels.contracts import MessageHandler
from bub.envelope import Envelope, field_of
from bub.turn import TurnState
from loguru import logger

from bub_mcp.config import MCPSettings
from bub_mcp.tools import MCP_TOOLS_STATE_KEY, loaded_tool_names, mcp_describe, render_tools_prompt

if TYPE_CHECKING:
    from bub.builtin.agent import Agent
    from bub.framework import BubFramework

TOOL_PREFIX = "mcp."
TEXT_OUTPUT_SCHEMA: dict[str, Any] = {"type": "string"}

type ResultMode = Literal["text", "structured", "wrapped"]
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


def _output_schema(
    remote_tool: mcp.types.Tool,
) -> tuple[dict[str, Any] | None, ResultMode]:
    """Return the Bub output schema and how to read results of a remote tool.

    Only tools declaring an ``outputSchema`` return structured content, so every
    other tool returns text and gets a ``str`` return type in code-mode stubs.
    """
    schema = getattr(remote_tool, "outputSchema", None)
    if not isinstance(schema, dict):
        return TEXT_OUTPUT_SCHEMA, "text"
    if not schema.get("x-fastmcp-wrap-result"):
        return schema, "structured"
    # FastMCP wraps non-object results as {"result": value}.
    inner = schema.get("properties", {}).get("result")
    if not isinstance(inner, dict):
        return None, "wrapped"
    if "$defs" in schema:
        inner = {**inner, "$defs": schema["$defs"]}
    return inner, "wrapped"


def _tool_result_value(result: Any, *, mode: ResultMode = "text") -> Any:
    """Return an MCP tool result as text or, for typed tools, its structured content."""
    structured = getattr(result, "structured_content", None)
    if mode == "text" or structured is None:
        return _format_tool_result(result)
    fastmcp_meta = (getattr(result, "meta", None) or {}).get("fastmcp")
    if isinstance(structured, dict) and (
        mode == "wrapped"
        or (isinstance(fastmcp_meta, dict) and fastmcp_meta.get("wrap_result"))
    ):
        return structured.get("result")
    return structured


def _render_tool_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


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

    rendered = "\n".join(blocks).strip()
    if rendered:
        return rendered

    is_error = bool(getattr(result, "is_error", False))
    return "error: remote MCP tool returned no content" if is_error else "ok"


@dataclass
class MCPServerState:
    client: Any | None = None
    tools: list[Tool] = field(default_factory=list)
    connected: bool = False
    error: str | None = None


class MCPChannel(Lifecycle):
    name = LIFECYCLE_CHANNEL_NAME
    stop_when_all_failed = True

    def __init__(self) -> None:
        self.settings = bub.ensure_config(MCPSettings)
        self._server_configs: dict[str, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()
        self._bootstrap_task: asyncio.Task[None] | None = None
        self._servers: dict[str, MCPServerState] = {}
        self._bindings: WeakKeyDictionary[
            Agent, dict[str, tuple[Tool, Tool | None]]
        ] = WeakKeyDictionary()
        self._stop_event: asyncio.Event | None = None

    @classmethod
    def from_server_configs(
        cls, server_configs: Mapping[str, Mapping[str, Any]]
    ) -> Self:
        channel = cls()
        channel._server_configs = {
            name: deepcopy(dict(server_config))
            for name, server_config in server_configs.items()
        }
        return channel

    async def start(self, stop_event: asyncio.Event) -> None:
        self._stop_event = stop_event
        if any(server.connected for server in self._servers.values()):
            return
        if self._bootstrap_task is not None and not self._bootstrap_task.done():
            return
        self._bootstrap_task = asyncio.create_task(
            self._bootstrap(stop_event), name="bub-mcp.bootstrap"
        )

    async def connect(self) -> None:
        """Wait for discovery when embedded without Bub's channel manager."""
        await self.start(asyncio.Event())
        if self._bootstrap_task is not None:
            await self._bootstrap_task

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

        for agent, bindings in list(self._bindings.items()):
            self._restore_tools(agent, bindings)
            if self in agent.catalogs:
                agent.catalogs.remove(self)
            if not any(isinstance(catalog, MCPChannel) for catalog in agent.catalogs):
                if agent.tools.get(mcp_describe.name) is mcp_describe:
                    agent.tools.pop(mcp_describe.name)
        self._bindings.clear()

        for client in clients:
            await self._close_client(client)

    @property
    def tools(self) -> dict[str, Tool]:
        """Tools from currently connected servers; never registered globally."""
        return {
            tool.name: tool
            for server in self._servers.values()
            if server.connected
            for tool in server.tools
            if self.settings.allows_tool(tool.name)
        }

    def bind_agent(self, agent: Agent) -> None:
        """Refresh the catalog without registering unloaded execution tools."""
        previous = self._bindings.pop(agent, {})
        tools = self.tools
        self._restore_tools(
            agent,
            {name: binding for name, binding in previous.items() if name not in tools},
        )
        bindings = {}
        for name, remote_tool in tools.items():
            original = previous[name][1] if name in previous else agent.tools.get(name)
            bindings[name] = (remote_tool, original)
            if name in previous and agent.tools.get(name) is previous[name][0]:
                agent.tools[name] = remote_tool
        self._bindings[agent] = bindings
        # Explicit tool sets may omit the globally registered discovery helper.
        agent.tools.setdefault(mcp_describe.name, mcp_describe)
        if self not in agent.catalogs:
            agent.catalogs.insert(-1, self)

    @staticmethod
    def _restore_tools(agent: Agent, bindings: dict[str, tuple[Tool, Tool | None]]) -> None:
        for name, (installed, original) in bindings.items():
            if agent.tools.get(name) is not installed:
                continue
            if original is None:
                agent.tools.pop(name, None)
            else:
                agent.tools[name] = original

    async def prepare(self, tools: list[Tool], tape: Tape) -> tuple[list[Tool], str]:
        """Select this source's scoped tools and contribute its discovery summary."""
        agent = tape.context.state["_runtime_agent"]
        first_source = next(catalog for catalog in agent.catalogs if isinstance(catalog, MCPChannel))
        if first_source is self:
            tape.context.state[MCP_TOOLS_STATE_KEY] = {}
        owned = self.tools
        available = {item.name: item for item in tools if owned.get(item.name) is item and item.agent_use}
        native = [item for item in tools if owned.get(item.name) is not item and item is not mcp_describe]
        tape.context.state[MCP_TOOLS_STATE_KEY].update(available)
        code_mode = tape.context.state.get("code_mode") and any(item.name == "run_code" for item in native)
        loaded = set(available) if code_mode else await loaded_tool_names(tape)
        selected = {name: item for name, item in available.items() if name in loaded}
        agent.tools.update(selected)
        native.extend(selected.values())
        if code_mode:
            return native, ""
        if tape.context.state[MCP_TOOLS_STATE_KEY]:
            native.append(mcp_describe)
        pending = [item for name, item in available.items() if name not in loaded]
        return native, render_tools_prompt(pending)

    async def bind_runtime_tools(
        self, framework: BubFramework, message: Envelope
    ) -> None:
        # Discovery may still be running when the first message arrives.
        if self._bootstrap_task is not None:
            await asyncio.shield(self._bootstrap_task)
        agent = field_of(message, "_runtime_agent")
        if agent is None:
            builtin = framework.plugin_manager.get_plugin("builtin")
            if builtin is None:
                return
            # Bub currently exposes its default Agent only through BuiltinImpl.
            agent = builtin._get_agent()
        self.bind_agent(agent)

    def list(self) -> dict[str, MCPServerState]:
        return self._servers.copy()

    async def add(self, name: str, server: dict[str, Any]) -> dict[str, dict[str, Any]]:
        if self._server_configs is not None:
            raise RuntimeError("externally configured MCP channels are read-only")
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
        if self._server_configs is not None:
            raise RuntimeError("externally configured MCP channels are read-only")
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
        self,
        server_name: str,
        remote_name: str,
        arguments: dict[str, Any],
        *,
        result_mode: ResultMode = "text",
    ) -> Any:
        server = self._servers.get(server_name)
        if server is None or server.client is None:
            raise RuntimeError(
                f"MCP client for server '{server_name}' is not connected"
            )
        result = await server.client.call_tool(remote_name, arguments or {})
        return _tool_result_value(result, mode=result_mode)

    def bootstrap(self) -> None:
        async def main() -> None:
            stop_event = asyncio.Event()
            await self._bootstrap(stop_event)

        asyncio.run(main())

    async def _bootstrap(self, stop_event: asyncio.Event) -> None:
        async with self._lock:
            try:
                config = self._read_mcp_servers()
                if not config:
                    self._servers = {}
                    return

                config_items = list(config.items())
                tasks = [
                    asyncio.create_task(
                        self._connect_server(server_name, server_config)
                    )
                    for server_name, server_config in config_items
                ]
                try:
                    server_states = await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if (
                            isinstance(result, MCPServerState)
                            and result.client is not None
                        ):
                            await self._close_client(result.client)
                    raise

                self._servers = {
                    server_name: server_state
                    for (server_name, _server_config), server_state in zip(
                        config_items, server_states, strict=False
                    )
                }

                if (
                    self.stop_when_all_failed
                    and self._servers
                    and not any(server.connected for server in self._servers.values())
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
                if self.stop_when_all_failed:
                    stop_event.set()

    def _read_mcp_servers(self) -> dict[str, Any]:
        if self._server_configs is not None:
            return deepcopy(self._server_configs)
        return self.settings.read_mcp_servers()

    def _create_client(
        self, server_name: str, server_config: dict[str, Any]
    ) -> fastmcp.Client[Any]:
        return _create_fastmcp_client(
            {server_name: server_config},
            init_timeout_seconds=self.settings.init_timeout_seconds,
        )

    async def _connect_server(
        self, server_name: str, server_config: dict[str, Any]
    ) -> MCPServerState:
        server = MCPServerState()
        client: Any | None = None
        try:
            client = self._create_client(server_name, server_config)
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
        output_schema, result_mode = _output_schema(remote_tool)
        return Tool(
            name=bub_name,
            description=str(remote_tool.description or f"MCP tool {remote_name}"),
            parameters=_tool_parameters(remote_tool),
            handler=self._make_handler(server_name, remote_name, result_mode),
            renderer=_render_tool_value,
            output_schema=output_schema,
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

    def _make_handler(
        self, server_name: str, remote_name: str, result_mode: ResultMode
    ):
        async def _handler(**payload: Any) -> Any:
            return await self.call_tool(
                server_name, remote_name, payload, result_mode=result_mode
            )

        return _handler

    @staticmethod
    async def _close_client(client: Any) -> None:
        with contextlib.suppress(Exception):
            # __aexit__ leaves keep-alive stdio transports running.
            await client.close()


class MCPPlugin:
    def __init__(self, framework: Any) -> None:
        self.framework = framework
        self._manager = MCPChannel()

    @hookimpl
    async def load_state(self, message: Envelope, session_id: str) -> TurnState:
        await self._manager.bind_runtime_tools(self.framework, message)
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


@final
class MCPServerInfo(TypedDict):
    connected: bool
    tools: list[str]
    error: str | None


@final
class MCPServerList(TypedDict):
    servers: dict[str, MCPServerInfo]


def _render_server_list(result: MCPServerList) -> str:
    servers = result["servers"]
    if not servers:
        return "No MCP servers configured."
    lines: list[str] = []
    lines.append("🔌 MCP Servers:")
    for name, server in servers.items():
        lines.append(f"- {name}")
        if server["connected"]:
            lines.append("  Status: Connected")
            lines.append(
                f"  Tools: {', '.join(server['tools']) if server['tools'] else 'No tools'}"
            )
        else:
            lines.append("  Status: Not connected")
            if server["error"]:
                lines.append(f"  Error: {server['error']}")
    return "\n".join(lines)


@tool(name="mcp", context=True, renderer=_render_server_list)
def mcp_list(*, context: ToolContext) -> MCPServerList:
    """List configured MCP servers."""
    manager = context.state.get("mcp")
    if not isinstance(manager, MCPChannel):
        raise RuntimeError("MCP channel is not available in state")
    return {
        "servers": {
            name: {
                "connected": server.connected,
                "tools": [tool.name for tool in server.tools],
                "error": server.error,
            }
            for name, server in manager.list().items()
        }
    }
