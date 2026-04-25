from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bub import hookimpl

from bub_acp.bridge import ACPBridge
from bub_acp.cli import make_acp_command
from bub_acp.config import ACPSettings

if TYPE_CHECKING:
    import typer
    from bub.framework import BubFramework
    from bub.types import State
    from republic import AsyncStreamEvents


class ACPPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework
        self.settings = ACPSettings()
        self.bridge = ACPBridge(self.settings)

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        app.add_typer(make_acp_command(self.settings), name="acp")

    @hookimpl
    async def run_model(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: State,
    ) -> str | None:
        return await self.bridge.run_model(prompt, session_id=session_id, state=state)

    @hookimpl
    async def run_model_stream(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: State,
    ) -> AsyncStreamEvents | None:
        return await self.bridge.run_model_stream(prompt, session_id=session_id, state=state)
