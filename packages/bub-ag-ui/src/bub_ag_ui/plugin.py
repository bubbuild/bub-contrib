from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from typing import Any

import bub
import typer
from bub import hookimpl
from bub.channels import Channel, Interface
from bub.channels.contracts import MessageHandler
from bub.channels.message import ChannelMessage
from bub.streaming import StreamEvent

from bub_ag_ui.config import AGUISettings
from bub_ag_ui.execution import install_execution_hooks, stream_events
from bub_ag_ui.http import run_server


class AGUIChannel(Interface):
    name = "ag-ui"

    def __init__(self, framework: Any) -> None:
        self.framework = framework
        self.task: asyncio.Task[None] | None = None
        self.stop_event = asyncio.Event()

    async def start(self, stop_event: asyncio.Event) -> None:
        if self.task is not None and not self.task.done():
            return
        self.stop_event = stop_event
        self.task = asyncio.create_task(
            run_server(
                self.framework,
                settings=bub.ensure_config(AGUISettings),
                channel_managed=True,
                shutdown_trigger=stop_event.wait,
            )
        )

        def stopped(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None:
                stop_event.set()

        self.task.add_done_callback(stopped)

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def stream_events(
        self, message: ChannelMessage, stream: AsyncIterable[StreamEvent]
    ) -> AsyncIterable[StreamEvent]:
        return stream_events(message, stream)


class AGUIPlugin:
    def __init__(self, framework: Any) -> None:
        self.framework = framework
        install_execution_hooks(framework)

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        return [AGUIChannel(self.framework)]

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        @app.command("ag-ui", help="Serve Bub over AG-UI HTTP/SSE.")
        def ag_ui(
            host: str | None = typer.Option(None),
            port: int | None = typer.Option(None, min=1, max=65535),
        ) -> None:
            settings = bub.ensure_config(AGUISettings)
            overrides = {
                key: value
                for key, value in {"host": host, "port": port}.items()
                if value is not None
            }
            asyncio.run(
                run_server(
                    self.framework, settings=settings.model_copy(update=overrides)
                )
            )
