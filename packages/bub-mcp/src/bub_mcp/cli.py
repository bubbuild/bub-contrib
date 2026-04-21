from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from bub_mcp.plugin import MCPChannel, MCPServerState


class MCPTransport(StrEnum):
    HTTP = "http"
    SSE = "sse"
    STDIO = "stdio"


def _format_server_list(servers: dict[str, MCPServerState]) -> str:
    lines: list[str] = []
    lines.append(typer.style("🔌 MCP Tools", bold=True))
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


def _parse_key_value(option_name: str, item: str, separator: str) -> tuple[str, str]:
    if separator not in item:
        raise typer.BadParameter(f"{option_name} must be in KEY{separator}VALUE format")
    key, value = item.split(separator, 1)
    key = key.strip()
    value = value.strip()
    if not key:
        raise typer.BadParameter(f"{option_name} key must not be blank")
    return key, value


def _build_stdio_server_config(
    command_args: list[str], env_items: list[str], header_items: list[str]
) -> dict[str, object]:
    if not command_args:
        raise typer.BadParameter("stdio transport requires a command after '--'")
    if header_items:
        raise typer.BadParameter("--header is only supported for http or sse transport")

    command, *args = command_args
    config: dict[str, object] = {"command": command}
    if args:
        config["args"] = args
    if env_items:
        config["env"] = {
            key: value
            for key, value in (
                _parse_key_value("--env", item, "=") for item in env_items
            )
        }
    return config


def _build_remote_server_config(
    transport: MCPTransport,
    targets: list[str],
    env_items: list[str],
    header_items: list[str],
) -> dict[str, object]:
    if env_items:
        raise typer.BadParameter("--env is only supported for stdio transport")
    if len(targets) != 1:
        raise typer.BadParameter(
            f"{transport.value} transport requires exactly one URL argument"
        )

    config: dict[str, object] = {"url": targets[0], "transport": transport.value}
    if header_items:
        config["headers"] = {
            key: value
            for key, value in (
                _parse_key_value("--header", item, ":") for item in header_items
            )
        }
    return config


def _build_server_config(
    transport: MCPTransport,
    targets: list[str],
    env_items: list[str],
    header_items: list[str],
) -> dict[str, object]:
    if transport is MCPTransport.STDIO:
        return _build_stdio_server_config(targets, env_items, header_items)
    return _build_remote_server_config(transport, targets, env_items, header_items)


def _probe_server(manager: MCPChannel, name: str) -> tuple[bool, str]:
    try:
        manager.bootstrap()
        server = manager.list().get(name)
        if server is None:
            return False, f"MCP server '{name}' is missing after bootstrap."
        rendered = _format_server_list({name: server})
        if not server.connected:
            error_message = server.error or "unknown connection error"
            return False, f"{rendered}\nConnection test failed: {error_message}"
        return True, rendered
    finally:
        asyncio.run(manager.stop())


def make_mcp_command(manager: MCPChannel) -> typer.Typer:
    app = typer.Typer()

    @app.command("list")
    def list_tools() -> None:
        """List registered MCP tools."""
        manager.bootstrap()
        mcp_tools = manager.list()
        if not mcp_tools:
            typer.echo("No MCP tools registered.")
            return
        typer.echo(_format_server_list(mcp_tools))

    @app.command("add")
    def add_server(
        transport: MCPTransport = typer.Option(
            ..., "--transport", help="MCP transport: http, sse, or stdio."
        ),
        env: list[str] | None = typer.Option(
            None,
            "--env",
            help="Environment variable in KEY=VALUE format for stdio servers.",
        ),
        header: list[str] | None = typer.Option(
            None,
            "--header",
            help="Header in 'Name: Value' format for http or sse servers.",
        ),
        name: str = typer.Argument(..., help="Server name."),
        target: list[str] = typer.Argument(
            ..., help="URL for remote servers, or '-- <command> [args...]' for stdio."
        ),
    ) -> None:
        """Add an MCP server configuration."""
        server_config = _build_server_config(
            transport,
            target,
            env or [],
            header or [],
        )
        try:
            asyncio.run(manager.add(name, server_config))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

        typer.echo(f"Added MCP server '{name}'.")
        typer.echo(f"Config: {manager.settings.config_path}")
        connected, output = _probe_server(manager, name)
        typer.echo("Connection test: ok" if connected else "Connection test: failed")
        typer.echo(output, err=not connected)
        if not connected:
            raise typer.Exit(code=1)

    @app.command("remove")
    def remove_server(
        name: str = typer.Argument(..., help="Server name."),
    ) -> None:
        """Remove an MCP server configuration."""
        try:
            asyncio.run(manager.remove(name))
        except KeyError as exc:
            typer.echo(f"MCP server '{name}' does not exist.", err=True)
            raise typer.Exit(code=1) from exc
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

        typer.echo(f"Removed MCP server '{name}'.")
        typer.echo(f"Config: {manager.settings.config_path}")

    return app
