from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from bub_acp.cli import make_acp_command
from bub_acp.config import ACPSettings


def test_cli_add_list_use_remove(tmp_path: Path) -> None:
    runner = CliRunner()
    settings = ACPSettings(config_path=tmp_path / "acp.json")
    app = make_acp_command(settings)

    add = runner.invoke(
        app,
        [
            "add",
            "--default",
            "--env",
            "FOO=bar",
            "fake",
            "python",
            "agent.py",
        ],
    )
    listing = runner.invoke(app, ["list"])
    use = runner.invoke(app, ["use", "fake"])
    remove = runner.invoke(app, ["remove", "fake"])

    assert add.exit_code == 0
    assert "Saved ACP agent 'fake'." in add.stdout
    assert listing.exit_code == 0
    assert "fake (default)" in listing.stdout
    assert use.exit_code == 0
    assert remove.exit_code == 0


def test_cli_serve_invokes_run_agent(monkeypatch) -> None:
    called: dict[str, object] = {}

    async def fake_run_agent(agent) -> None:
        called["agent"] = agent

    monkeypatch.setattr("bub_acp.cli.acp.run_agent", fake_run_agent)
    app = make_acp_command(ACPSettings(config_path=Path("/tmp/unused-acp.json")))
    runner = CliRunner()

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert called["agent"].__class__.__name__ == "BubACPServerAgent"
