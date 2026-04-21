from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bub_mcp import cli, plugin
from typer.testing import CliRunner

runner = CliRunner()


def _make_app(tmp_path: Path) -> tuple[object, plugin.MCPChannel]:
    manager = plugin.MCPChannel()
    manager.settings.config_path = tmp_path / "mcp.json"
    return cli.make_mcp_command(manager), manager


def _make_tool(name: str) -> plugin.Tool:
    async def fake_handler(**payload: Any) -> str:
        del payload
        return "ok"

    return plugin.Tool(
        name=name,
        description=f"Tool {name}",
        parameters={"type": "object", "properties": {}},
        handler=fake_handler,
    )


def test_add_http_server_command_persists_remote_config(tmp_path: Path) -> None:
    app, manager = _make_app(tmp_path)

    def fake_bootstrap() -> None:
        manager._servers = {
            "weather": plugin.MCPServerState(
                connected=True,
                tools=[_make_tool("mcp.weather_get_forecast")],
            )
        }

    async def fake_stop() -> None:
        return None

    manager.bootstrap = fake_bootstrap
    manager.stop = fake_stop

    result = runner.invoke(
        app,
        [
            "add",
            "--transport",
            "http",
            "weather",
            "https://weather.example.com/mcp",
        ],
    )

    assert result.exit_code == 0
    assert "Added MCP server 'weather'." in result.stdout
    assert "Connection test: ok" in result.stdout
    assert "mcp.weather_get_forecast" in result.stdout
    assert manager.settings.read_mcp_servers() == {
        "weather": {
            "url": "https://weather.example.com/mcp",
            "transport": "http",
        }
    }


def test_add_stdio_server_command_persists_command_and_env(tmp_path: Path) -> None:
    app, manager = _make_app(tmp_path)

    def fake_bootstrap() -> None:
        manager._servers = {
            "filesystem": plugin.MCPServerState(
                connected=True,
                tools=[_make_tool("mcp.filesystem_read")],
            )
        }

    async def fake_stop() -> None:
        return None

    manager.bootstrap = fake_bootstrap
    manager.stop = fake_stop

    result = runner.invoke(
        app,
        [
            "add",
            "--transport",
            "stdio",
            "--env",
            "API_KEY=secret",
            "filesystem",
            "--",
            "npx",
            "-y",
            "@modelcontextprotocol/server-filesystem",
            "/tmp",
        ],
    )

    assert result.exit_code == 0
    assert "Connection test: ok" in result.stdout
    assert "mcp.filesystem_read" in result.stdout
    assert manager.settings.read_mcp_servers() == {
        "filesystem": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {"API_KEY": "secret"},
        }
    }


def test_add_http_server_command_persists_headers(tmp_path: Path) -> None:
    app, manager = _make_app(tmp_path)

    def fake_bootstrap() -> None:
        manager._servers = {
            "secure-api": plugin.MCPServerState(
                connected=True,
                tools=[_make_tool("mcp.secure_api_ping")],
            )
        }

    async def fake_stop() -> None:
        return None

    manager.bootstrap = fake_bootstrap
    manager.stop = fake_stop

    result = runner.invoke(
        app,
        [
            "add",
            "--transport",
            "http",
            "--header",
            "Authorization: Bearer token",
            "secure-api",
            "https://api.example.com/mcp",
        ],
    )

    assert result.exit_code == 0
    assert manager.settings.read_mcp_servers() == {
        "secure-api": {
            "url": "https://api.example.com/mcp",
            "transport": "http",
            "headers": {"Authorization": "Bearer token"},
        }
    }


def test_add_server_command_fails_when_connection_test_fails(tmp_path: Path) -> None:
    app, manager = _make_app(tmp_path)

    def fake_bootstrap() -> None:
        manager._servers = {
            "broken": plugin.MCPServerState(
                connected=False,
                error="connection refused",
            )
        }

    async def fake_stop() -> None:
        return None

    manager.bootstrap = fake_bootstrap
    manager.stop = fake_stop

    result = runner.invoke(
        app,
        [
            "add",
            "--transport",
            "http",
            "broken",
            "https://broken.example.com/mcp",
        ],
    )

    assert result.exit_code == 1
    assert "Added MCP server 'broken'." in result.stdout
    assert "Connection test: failed" in result.stdout
    assert "connection refused" in result.stderr
    assert manager.settings.read_mcp_servers() == {
        "broken": {
            "url": "https://broken.example.com/mcp",
            "transport": "http",
        }
    }


def test_remove_server_command_deletes_configured_server(tmp_path: Path) -> None:
    app, manager = _make_app(tmp_path)
    manager.settings.config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "weather": {
                        "url": "https://weather.example.com/mcp",
                        "transport": "http",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["remove", "weather"])

    assert result.exit_code == 0
    assert "Removed MCP server 'weather'." in result.stdout
    assert manager.settings.read_mcp_servers() == {}


def test_remove_server_command_fails_for_missing_server(tmp_path: Path) -> None:
    app, _manager = _make_app(tmp_path)

    result = runner.invoke(app, ["remove", "missing"])

    assert result.exit_code == 1
    assert "MCP server 'missing' does not exist." in result.stderr
