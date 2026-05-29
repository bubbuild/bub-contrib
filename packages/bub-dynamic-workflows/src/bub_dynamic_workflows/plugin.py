from __future__ import annotations

import typer
from bub import hookimpl
from bub.channels import Channel
from bub.framework import BubFramework
from bub.types import Envelope, MessageHandler, State

from bub_dynamic_workflows.cli import register_cli_commands
from bub_dynamic_workflows.channel import WorkflowChannel


class DynamicWorkflowsPlugin:
    def __init__(self, framework: BubFramework | None = None) -> None:
        from bub_dynamic_workflows import tools  # noqa: F401

        self.channel = WorkflowChannel(framework) if framework is not None else None

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> State:
        del message, session_id
        return {"workflow": self.channel} if self.channel is not None else {}

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        register_cli_commands(app)

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        del message_handler
        return [self.channel] if self.channel is not None else []
