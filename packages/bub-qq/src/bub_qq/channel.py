"""QQ channel with auth, OpenAPI and pluggable receive transports."""

from __future__ import annotations

import asyncio
from typing import Any

from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.types import MessageHandler
from loguru import logger

from .auth import QQTokenProvider
from .c2c import build_c2c_channel_message
from .c2c import is_passive_reply_window_open
from .c2c import next_c2c_msg_seq
from .c2c import QQC2CDeduper
from .c2c import QQC2CSessionState
from .c2c import remember_c2c_session
from .c2c import resolve_c2c_openid
from .config import QQConfig
from .models import QQC2CMessage
from .openapi import QQOpenAPI
from .openapi_errors import QQOpenAPIError
from .send_errors import log_send_error
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
        )

    @property
    def needs_debounce(self) -> bool:
        return True

    async def start(self, stop_event: Any) -> None:
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

        websocket_stop = stop_event if isinstance(stop_event, asyncio.Event) else None
        await self._websocket.start(websocket_stop)
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
        content = (message.content or "").strip()
        if not content:
            logger.warning("qq.send skip_empty session_id={}", message.session_id)
            return

        session_id = message.session_id or ""
        chat_id = message.chat_id or ""
        openid = resolve_c2c_openid(channel_name=self.name, session_id=session_id, chat_id=chat_id)
        if not openid:
            logger.warning(
                "qq.send unresolved_openid session_id={} chat_id={}",
                message.session_id,
                message.chat_id,
            )
            return

        msg_id = self._c2c_state.latest_message_id_by_session.get(session_id)
        if not msg_id:
            logger.warning(
                "qq.send missing_msg_id session_id={} reason=active_push_not_supported",
                session_id,
            )
            return

        if not is_passive_reply_window_open(self._c2c_state, session_id):
            logger.warning(
                "qq.send passive_reply_window_expired session_id={} msg_id={}",
                session_id,
                msg_id,
            )
            return

        msg_seq = next_c2c_msg_seq(self._c2c_state, session_id)
        try:
            result = await self._openapi.post_c2c_text_message(
                openid=openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
        except QQOpenAPIError as exc:
            log_send_error(
                exc,
                session_id=session_id,
                openid=openid,
                msg_id=msg_id,
                msg_seq=msg_seq,
                receive_mode=self._config.receive_mode,
            )
            return

        logger.info(
            "qq.send success session_id={} openid={} msg_id={} msg_seq={} response_id={}",
            session_id,
            openid,
            msg_id,
            msg_seq,
            result.get("id"),
        )

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
        try:
            message = QQC2CMessage.from_event(payload)
        except ValueError as exc:
            logger.warning("qq.c2c.invalid_payload error={}", exc)
            return

        if self._c2c_deduper.seen(message.message_id):
            logger.info("qq.c2c.duplicate message_id={}", message.message_id)
            return

        channel_message = build_c2c_channel_message(self.name, message)
        remember_c2c_session(
            self._c2c_state,
            session_id=channel_message.session_id,
            message_id=message.message_id,
            timestamp=message.timestamp,
            sequence=message.sequence,
        )
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
