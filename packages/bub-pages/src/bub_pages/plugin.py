from __future__ import annotations

from typing import Any

import typer
from bub import hookimpl

from bub_pages.cli import make_pages_command
from bub_pages.config import PagesSettings


class PagesPlugin:
    def __init__(self, framework: Any) -> None:
        del framework
        self.settings = PagesSettings.from_env()

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        app.add_typer(
            make_pages_command(self.settings),
            name="pages",
            help="Manage static pages sites",
        )
