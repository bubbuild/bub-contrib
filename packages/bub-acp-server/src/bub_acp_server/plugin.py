from __future__ import annotations

import asyncio
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from bub import hookimpl
from bub.channels import Channel
from bub.channels.contracts import MessageHandler
from bub.envelope import Envelope, field_of
from bub.turn import TurnState

from bub_acp_server.agent import (
    ACPInboundMessage,
    BubACPAgent,
    run_acp_agent,
)
from bub_acp_server.steering import ACPSteeringInbox

if TYPE_CHECKING:
    from bub.framework import BubFramework

__all__ = ["ACPServerPlugin", "BubACPAgent", "run_acp_agent"]


class Transport(str, Enum):
    stdio = "stdio"
    http = "http"
    websocket = "websocket"


class ACPServerPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework
        self.steering_inbox = ACPSteeringInbox()

    @hookimpl(tryfirst=True)
    def provide_steering_inbox(self) -> ACPSteeringInbox:
        return self.steering_inbox

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        del message_handler
        try:
            from bub_acp_server.http import ACPHTTPChannel
        except ImportError:
            return []  # HTTP is optional; stdio remains usable without its extra.
        return [ACPHTTPChannel(self.framework)]

    @hookimpl(specname="load_state", trylast=True)
    def bind_session_mcp_tools(self, message: Envelope) -> None:
        # Apply session tools after process-configured MCP providers have bound theirs.
        if (
            isinstance(message, ACPInboundMessage)
            and message._mcp_channel is not None
            and message._runtime_agent is not None
        ):
            message._mcp_channel.bind_agent(message._runtime_agent)

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> TurnState:
        del session_id
        context = field_of(message, "context", {})
        if not isinstance(context, Mapping):
            return {}
        state: TurnState = {}
        workspace = context.get("_runtime_workspace")
        if isinstance(workspace, str) and workspace:
            state["_runtime_workspace"] = workspace
        model = context.get("_runtime_model")
        if isinstance(model, str) and model:
            state["model"] = model
        reasoning_effort = context.get("_runtime_reasoning_effort")
        if isinstance(reasoning_effort, str) and reasoning_effort:
            state["reasoning_effort"] = reasoning_effort
        return state

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        @app.command(
            "acp", help="Run Bub as an ACP agent over stdio, HTTP or WebSocket."
        )
        def acp(
            command: str | None = typer.Argument(None, metavar="[serve]"),
            transport: Transport = typer.Option(Transport.stdio, help="ACP transport."),
            host: str = typer.Option(
                "127.0.0.1", help="HTTP/WebSocket listen address."
            ),
            port: int = typer.Option(
                28200, min=1, max=65535, help="HTTP/WebSocket listen port."
            ),
            certfile: Path | None = typer.Option(
                None,
                exists=True,
                dir_okay=False,
                help="TLS certificate for HTTPS/HTTP2 or WSS.",
            ),
            keyfile: Path | None = typer.Option(
                None, exists=True, dir_okay=False, help="TLS private key."
            ),
        ) -> None:
            if command == "serve":
                typer.echo(
                    "Warning: `bub acp serve` is deprecated; use `bub acp` instead.",
                    err=True,
                )
            elif command is not None:
                raise typer.BadParameter(
                    f"Got unexpected extra argument {command!r}", param_hint="command"
                )
            if transport == Transport.stdio:
                if (
                    host != "127.0.0.1"
                    or port != 28200
                    or certfile is not None
                    or keyfile is not None
                ):
                    raise typer.BadParameter(
                        "Listen and TLS options require --transport http or websocket"
                    )
                asyncio.run(run_acp_agent(self.framework))
                return
            if (certfile is None) != (keyfile is None):
                raise typer.BadParameter(
                    "--certfile and --keyfile must be provided together"
                )
            try:
                from bub_acp_server.http import run_acp_http
            except ImportError as error:
                raise typer.BadParameter(
                    "HTTP/WebSocket requires the http extra: uv pip install 'bub-acp-server[http]'"
                ) from error
            # The SDK serves HTTP and WebSocket on the same /acp endpoint.
            asyncio.run(
                run_acp_http(
                    self.framework,
                    host=host,
                    port=port,
                    certfile=certfile,
                    keyfile=keyfile,
                )
            )
