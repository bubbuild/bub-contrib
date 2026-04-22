from __future__ import annotations

import typer
from bub import hookimpl

from tape_dataset_opendal.cli import export_command


@hookimpl
def register_cli_commands(app: typer.Typer) -> None:
    app.command("tape-export")(export_command)
