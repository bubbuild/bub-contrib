from __future__ import annotations

import asyncio
import json
from asyncio import Event

from aiohttp import web
from loguru import logger
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.types import MessageHandler


class HttpBridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUB_HTTP_BRIDGE_", extra="ignore", env_file=".env")

    port: int = Field(default=9800, description="Port to listen on")
    host: str = Field(default="127.0.0.1", description="Host to bind to")


class HttpBridgeChannel(Channel):
    name = "http-bridge"

    def __init__(self, on_receive: MessageHandler) -> None:
        self._on_receive = on_receive
        self._settings = HttpBridgeSettings()
        self._app = web.Application()
        self._app.router.add_post("/message", self._handle_message)
        self._runner: web.AppRunner | None = None

    async def _handle_message(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid JSON"}, status=400)

        session_id = body.get("session_id")
        content = body.get("content")
        source = body.get("source", "unknown")

        if not session_id or not content:
            return web.json_response(
                {"error": "session_id and content are required"}, status=400
            )

        # Determine output channel and chat_id from session_id
        if ":" in session_id:
            output_channel, chat_id = session_id.split(":", 1)
        else:
            output_channel = "http-bridge"
            chat_id = session_id

        message = ChannelMessage(
            session_id=session_id,
            channel="http-bridge",
            chat_id=chat_id,
            content=content,
            is_active=True,
            output_channel=output_channel,
            context={"source": source},
        )

        await self._on_receive(message)
        return web.json_response({"status": "accepted"}, status=202)

    async def start(self, stop_event: Event) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            self._settings.host,
            self._settings.port,
        )
        await site.start()
        logger.info(
            f"http-bridge listening on {self._settings.host}:{self._settings.port}"
        )

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("http-bridge stopped")