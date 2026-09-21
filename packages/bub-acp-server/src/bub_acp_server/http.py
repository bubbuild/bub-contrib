"""ACP web transport using the SDK's ASGI adapter and Hypercorn."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakSet

import bub
from acp.http.asgi import create_asgi_app
from acp.interfaces import Client
from bub.channels import Interface
from bub.channels.message import ChannelMessage
from bub.streaming import StreamEvent
from hypercorn.asyncio import serve
from hypercorn.config import Config
from loguru import logger

from bub_acp_server.agent import ACPSession, BubACPAgent, active_stream_router
from bub_acp_server.config import ACPServerSettings
from bub_mcp.plugin import MCPChannel
from bub_acp_server.steering import ACPSteeringInbox

if TYPE_CHECKING:
    from bub.framework import BubFramework


def create_http_app(
    framework: BubFramework, *, channel_managed: bool = False
) -> Callable:
    """Create a /acp endpoint; the caller owns framework.running()."""
    prompt_lock = asyncio.Lock()
    sessions: dict[str, ACPSession] | None = None
    agents: WeakSet[BubACPAgent] = WeakSet()
    mcp_channels: dict[str, MCPChannel] = {}
    prompt_runs = {}
    closing_sessions: set[str] = set()

    def agent_factory(client: Client) -> BubACPAgent:
        nonlocal sessions
        inbox = framework.get_steering_inbox()
        agent = BubACPAgent(
            framework,
            steering_inbox=inbox if isinstance(inbox, ACPSteeringInbox) else None,
            prompt_lock=prompt_lock,
            sessions=sessions,
            mcp_channels=mcp_channels,
            prompt_runs=prompt_runs,
            closing_sessions=closing_sessions,
            bind_router=not channel_managed,
            # Match the SDK web adapter's stable-only protocol routes.
            use_unstable_protocol=False,
        )
        sessions = agent._sessions
        agents.add(agent)
        return agent

    sdk_app = create_asgi_app(agent_factory)

    async def cleanup() -> None:
        await asyncio.gather(*(agent.shutdown() for agent in list(agents)))
        # Session resources can outlive their originating HTTP connection.
        channels = list(mcp_channels.values())
        mcp_channels.clear()
        await asyncio.gather(*(channel.stop() for channel in channels))

    async def app(scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "lifespan":
            await sdk_app(scope, receive, send)
            return
        cleaned = False

        async def lifecycle_send(message: dict[str, Any]) -> None:
            nonlocal cleaned
            if message["type"] in {
                "lifespan.shutdown.complete",
                "lifespan.shutdown.failed",
            }:
                # Hypercorn may cancel remaining tasks as soon as it receives completion.
                await cleanup()
                cleaned = True
            await send(message)

        try:
            await sdk_app(scope, receive, lifecycle_send)
        finally:
            if not cleaned:
                await cleanup()

    return app


class ACPHTTPChannel(Interface):
    name = "acp-server"

    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework
        self.settings = bub.ensure_config(ACPServerSettings)
        self.name = self.settings.channel_name
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    async def start(self, stop_event: asyncio.Event) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = stop_event
        self._task = asyncio.create_task(
            run_acp_http(
                self.framework,
                host=self.settings.host,
                port=self.settings.port,
                certfile=self.settings.certfile,
                keyfile=self.settings.keyfile,
                channel_managed=True,
                shutdown_trigger=stop_event.wait,
            ),
            name="bub-acp-server.http",
        )

        def stopped(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and (error := task.exception()) is not None:
                logger.opt(exception=error).error("ACP HTTP server failed")
                stop_event.set()

        self._task.add_done_callback(stopped)

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                # Failures are reported by the done callback. Do not prevent
                # the gateway from shutting down its remaining channels.
                await asyncio.gather(self._task, return_exceptions=True)
            finally:
                self._task = None

    def stream_events(
        self, message: ChannelMessage, stream: AsyncIterable[StreamEvent]
    ) -> AsyncIterable[StreamEvent]:
        router = active_stream_router.get()
        return router.wrap_stream(message, stream) if router is not None else stream

    async def send(self, message: ChannelMessage) -> None:
        if (router := active_stream_router.get()) is not None:
            await router.dispatch_output(message)


async def run_acp_http(
    framework: BubFramework,
    *,
    host: str = "127.0.0.1",
    port: int = 28200,
    certfile: Path | None = None,
    keyfile: Path | None = None,
    channel_managed: bool = False,
    shutdown_trigger: Callable[[], Awaitable[None]] | None = None,
) -> None:
    config = Config()
    config.bind = [f"{host}:{port}"]
    if certfile is not None:
        config.certfile = str(certfile)
        config.keyfile = str(keyfile) if keyfile is not None else None
    async with nullcontext() if channel_managed else framework.running():
        await serve(
            create_http_app(framework, channel_managed=channel_managed),
            config,
            shutdown_trigger=shutdown_trigger,
        )
