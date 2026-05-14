from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import bub
from bub import hookimpl
from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.types import MessageHandler
from fastmcp import FastMCP
from loguru import logger

from bub_mcp_server.config import MCPServerSettings

if TYPE_CHECKING:
    from bub.framework import BubFramework


class MCPServerChannel(Channel):
    name = "mcp-server"

    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework
        self.settings = bub.ensure_config(MCPServerSettings)
        self._server: FastMCP | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def server(self) -> FastMCP | None:
        return self._server

    async def start(self, stop_event: asyncio.Event) -> None:
        del stop_event
        if self._task is not None and not self._task.done():
            return
        self._server = self._build_server()
        self._task = asyncio.create_task(
            self._run_server(self._server), name="bub-mcp-server.sse"
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _build_server(self) -> FastMCP:
        server = FastMCP(name="bub")

        @server.tool(
            name="run_model",
            description="Run one prompt through Bub and return the model output.",
        )
        async def run_model(prompt: str, session_id: str = "mcp:default") -> str:
            return await self.run_model(prompt=prompt, session_id=session_id)

        return server

    async def _run_server(self, server: FastMCP) -> None:
        logger.info(
            "starting bub MCP SSE server on http://{}:{}{}",
            self.settings.host,
            self.settings.port,
            self.settings.path,
        )
        await server.run_async(
            transport="sse",
            host=self.settings.host,
            port=self.settings.port,
            path=self.settings.path,
            log_level=self.settings.log_level,
            show_banner=False,
        )

    async def run_model(self, *, prompt: str, session_id: str) -> str:
        normalized_session_id = session_id.strip() or "mcp:default"
        inbound = ChannelMessage(
            session_id=normalized_session_id,
            channel=self.name,
            chat_id=normalized_session_id,
            content=prompt,
            is_active=True,
            kind="normal",
        )
        result = await self.framework.process_inbound(inbound)
        return result.model_output


class MCPServerPlugin:
    def __init__(self, framework: Any) -> None:
        self._channel = MCPServerChannel(framework)

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        del message_handler
        return [self._channel]
