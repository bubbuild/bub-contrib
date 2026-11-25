from __future__ import annotations

import sys
from pathlib import Path

import pytest
from bub_mcp.plugin import MCPManager
from prompt_toolkit.patch_stdout import patch_stdout

NOISY_SERVER = r"""
import os
from mcp.server.fastmcp import FastMCP

os.write(2, b"startup diagnostic\n")
server = FastMCP("noisy")

@server.tool()
def ping() -> str:
    os.write(2, b"tool diagnostic\n")
    return "pong"

server.run(transport="stdio")
"""


@pytest.mark.asyncio
async def test_stdio_stderr_is_logged_without_polluting_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    manager = MCPManager.from_server_configs(
        {"noisy": {"command": sys.executable, "args": ["-c", NOISY_SERVER]}}
    )

    with patch_stdout(raw=True):
        try:
            await manager.connect()
            assert await manager.call_tool("noisy", "ping", {}) == "pong"
        finally:
            await manager.stop()

    captured = capfd.readouterr()
    terminal = captured.out + captured.err
    assert "startup diagnostic" not in terminal
    assert "tool diagnostic" not in terminal
    log = (tmp_path / "logs" / "mcp" / "stderr.log").read_text(encoding="utf-8")
    assert "startup diagnostic" in log
    assert "tool diagnostic" in log


@pytest.mark.asyncio
async def test_stdio_startup_failure_leaves_a_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    manager = MCPManager.from_server_configs(
        {
            "broken": {
                "command": sys.executable,
                "args": ["-c", "raise RuntimeError('startup crash')"],
            }
        }
    )
    try:
        await manager.connect()
        assert manager.list()["broken"].error
    finally:
        await manager.stop()

    captured = capfd.readouterr()
    assert "startup crash" not in captured.out + captured.err
    log = (tmp_path / "logs" / "mcp" / "stderr.log").read_text(encoding="utf-8")
    assert "RuntimeError: startup crash" in log
