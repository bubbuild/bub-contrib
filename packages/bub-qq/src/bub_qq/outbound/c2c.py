from __future__ import annotations

from typing import Protocol

from bub.channels.message import ChannelMessage
from loguru import logger

from ..inbound.c2c import resolve_c2c_openid
from ..session import QQSessionState
from .send_flow import DEFAULT_PASSIVE_REPLIES_PER_MSG_ID
from .send_flow import DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS
from .send_flow import normalize_outbound_content
from .send_flow import run_send_flow


class QQC2COpenAPI(Protocol):
    async def post_c2c_text_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...

    async def post_c2c_markdown_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...


class QQC2CSendService:
    def __init__(
        self,
        *,
        channel_name: str,
        receive_mode: str,
        state: QQSessionState,
        openapi: QQC2COpenAPI,
        passive_reply_window_seconds: float = DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS,
        passive_replies_per_msg_id: int = DEFAULT_PASSIVE_REPLIES_PER_MSG_ID,
    ) -> None:
        self._channel_name = channel_name
        self._receive_mode = receive_mode
        self._state = state
        self._openapi = openapi
        self._passive_reply_window_seconds = passive_reply_window_seconds
        self._passive_replies_per_msg_id = passive_replies_per_msg_id

    async def send(self, message: ChannelMessage) -> dict[str, object] | None:
        content = normalize_outbound_content(message.content or "")
        if not content:
            logger.warning("qq.send skip_empty session_id={}", message.session_id)
            return None

        session_id = message.session_id or ""
        openid = resolve_c2c_openid(
            channel_name=self._channel_name,
            session_id=session_id,
            chat_id=message.chat_id or "",
        )
        if not openid:
            logger.warning(
                "qq.send unresolved_openid session_id={} chat_id={}",
                message.session_id,
                message.chat_id,
            )
            return None

        async def send_text(
            *, content: str, msg_id: str, msg_seq: int
        ) -> dict[str, object]:
            return await self._openapi.post_c2c_text_message(
                openid=openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

        async def send_markdown(
            *, content: str, msg_id: str, msg_seq: int
        ) -> dict[str, object]:
            return await self._openapi.post_c2c_markdown_message(
                openid=openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

        return await run_send_flow(
            state=self._state,
            receive_mode=self._receive_mode,
            session_id=session_id,
            target_openid=openid,
            content=content,
            send_text=send_text,
            send_markdown=send_markdown,
            passive_reply_window_seconds=self._passive_reply_window_seconds,
            passive_replies_per_msg_id=self._passive_replies_per_msg_id,
        )
