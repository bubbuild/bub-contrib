from __future__ import annotations

from typer.testing import CliRunner

from bub_dynamic_workflows.cli import app


def test_cli_start_does_not_offer_background_mode() -> None:
    result = CliRunner().invoke(app, ["start", "workflow.yaml", "--background"])

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_cli_does_not_offer_validate_command() -> None:
    result = CliRunner().invoke(app, ["validate", "workflow.yaml"])

    assert result.exit_code != 0
    assert "No such command" in result.output
