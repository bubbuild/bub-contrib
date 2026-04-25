from __future__ import annotations

import asyncio
import json
from pathlib import Path

import acp
import typer

from bub_acp.config import ACPAgentProcessConfig, ACPSettings
from bub_acp.server import make_server_agent


def _parse_env(item: str) -> tuple[str, str]:
    if "=" not in item:
        raise typer.BadParameter("--env must use KEY=VALUE")
    key, value = item.split("=", 1)
    key = key.strip()
    if not key:
        raise typer.BadParameter("--env key must not be blank")
    return key, value


def _render_list(settings: ACPSettings) -> str:
    config = settings.read_config()
    if not config.agents:
        return "No ACP agents configured."

    lines = ["ACP Agents"]
    for name, agent in config.agents.items():
        suffix = " (default)" if config.default_agent == name else ""
        command = " ".join([agent.command, *agent.args]).strip()
        lines.append(f"- {name}{suffix}")
        lines.append(f"  Command: {command}")
        if agent.cwd:
            lines.append(f"  Cwd: {agent.cwd}")
        if agent.env:
            lines.append(f"  Env: {json.dumps(agent.env, sort_keys=True)}")
    return "\n".join(lines)


def make_acp_command(settings: ACPSettings | None = None) -> typer.Typer:
    settings = settings or ACPSettings()
    app = typer.Typer()

    @app.command("serve")
    def serve() -> None:
        """Run Bub as an ACP server over stdio."""
        asyncio.run(acp.run_agent(make_server_agent()))

    @app.command("list")
    def list_agents() -> None:
        """List configured outbound ACP agents."""
        typer.echo(_render_list(settings))

    @app.command("add")
    def add_agent(
        default: bool = typer.Option(False, "--default", help="Set as default ACP agent."),
        cwd: Path | None = typer.Option(None, "--cwd", help="Working directory for the ACP process."),
        env: list[str] | None = typer.Option(None, "--env", help="Environment variable in KEY=VALUE format."),
        name: str = typer.Argument(..., help="ACP agent name."),
        command: list[str] = typer.Argument(..., help="Command after '--'."),
    ) -> None:
        """Add or update an ACP agent configuration."""
        if not command:
            raise typer.BadParameter("ACP command is required after '--'")
        env_map = dict(_parse_env(item) for item in (env or []))
        agent = ACPAgentProcessConfig(
            command=command[0],
            args=command[1:],
            env=env_map,
            cwd=None if cwd is None else str(cwd.resolve()),
        )
        settings.upsert_agent(name, agent, make_default=default)
        typer.echo(f"Saved ACP agent '{name}'.")
        typer.echo(f"Config: {settings.config_path}")

    @app.command("remove")
    def remove_agent(name: str = typer.Argument(..., help="ACP agent name.")) -> None:
        """Remove an ACP agent configuration."""
        try:
            settings.remove_agent(name)
        except KeyError as exc:
            typer.echo(f"ACP agent '{name}' does not exist.", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"Removed ACP agent '{name}'.")

    @app.command("use")
    def use_agent(name: str = typer.Argument(..., help="ACP agent name.")) -> None:
        """Set the default outbound ACP agent."""
        try:
            settings.use_agent(name)
        except KeyError as exc:
            typer.echo(f"ACP agent '{name}' does not exist.", err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"Default ACP agent: {name}")

    return app
