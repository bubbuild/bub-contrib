"""QQ channel with auth, OpenAPI and pluggable receive transports."""

from __future__ import annotations

import asyncio
from typing import Any

from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.types import MessageHandler
from loguru import logger

from .auth import QQTokenProvider
from .c2c import QQC2CDeduper
from .c2c import QQC2CInboundService
from .c2c import QQC2CSendService
from .c2c import QQC2CSessionState
from .config import QQConfig
from .openapi import QQOpenAPI
from .webhook import QQWebhookServer
from .websocket import QQWebSocketClient


class QQChannel(Channel):
    """QQ channel registration with reusable auth and OpenAPI client."""

    name = "qq"

    def __init__(self, on_receive: MessageHandler) -> None:
        self._on_receive = on_receive
        self._config = QQConfig()
        self._token_provider = QQTokenProvider(self._config)
        self._openapi = QQOpenAPI(self._config, self._token_provider)
        self._webhook = QQWebhookServer(self._config, self._handle_transport_payload)
        self._websocket = QQWebSocketClient(self._config, self._openapi, self._handle_transport_payload)
        self._c2c_deduper = QQC2CDeduper(self._config.inbound_dedupe_size)
        self._c2c_state = QQC2CSessionState(
            latest_message_id_by_session={},
            latest_sequence_by_session={},
            latest_timestamp_by_session={},
            send_record_by_session_and_msg_id={},
        )
        self._c2c_inbound = QQC2CInboundService(
            channel_name=self.name,
            deduper=self._c2c_deduper,
            state=self._c2c_state,
        )
        self._c2c_send = QQC2CSendService(
            channel_name=self.name,
            receive_mode=self._config.receive_mode,
            state=self._c2c_state,
            openapi=self._openapi,
        )

    @property
    def needs_debounce(self) -> bool:
        return True

    async def start(self, stop_event: asyncio.Event | None) -> None:
        if not self._config.appid or not self._config.secret:
            raise RuntimeError("qq appid/secret is empty")

        mode = self._normalize_receive_mode()
        if mode == "webhook":
            await self._webhook.start()
            logger.info(
                "qq.start mode=webhook token_url={} openapi_base_url={} webhook=http://{}:{}{}",
                self._config.token_url,
                self._config.openapi_base_url,
                self._config.webhook_host,
                self._config.webhook_port,
                self._config.webhook_path,
            )
            return

        await self._websocket.start(stop_event)
        logger.info(
            "qq.start mode=websocket token_url={} openapi_base_url={} intents={}",
            self._config.token_url,
            self._config.openapi_base_url,
            self._config.websocket_intents,
        )

    async def stop(self) -> None:
        await self._webhook.stop()
        await self._websocket.stop()
        await self._openapi.aclose()
        logger.info("qq.stopped")

    async def send(self, message: ChannelMessage) -> None:
        await self._c2c_send.send(message)

    async def _handle_transport_payload(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        event_type = payload.get("t")
        if op != 0:
            logger.info("qq.transport.ignored op={} t={}", op, event_type)
            return
        if event_type == "READY":
            logger.info("qq.websocket.ready")
            return
        if event_type == "RESUMED":
            logger.info("qq.websocket.resumed")
            return
        if event_type == "C2C_MESSAGE_CREATE":
            await self._handle_c2c_message(payload)
            return
        logger.info("qq.transport.unhandled event={} op={}", event_type, op)

    async def _handle_c2c_message(self, payload: dict[str, Any]) -> None:
        parsed = self._c2c_inbound.parse_inbound(payload)
        if parsed is None:
            return
        message, channel_message = parsed
        logger.info(
            "qq.c2c.inbound session_id={} user_openid={} content_len={} attachments={}",
            channel_message.session_id,
            message.user_openid,
            len(message.content),
            len(message.attachments),
        )
        await self._on_receive(channel_message)

    def _normalize_receive_mode(self) -> str:
        mode = (self._config.receive_mode or "").strip().lower()
        if mode not in {"webhook", "websocket"}:
            raise RuntimeError(
                f"qq receive_mode must be webhook or websocket, got {self._config.receive_mode!r}"
            )
        return mode
