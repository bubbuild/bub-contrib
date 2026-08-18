from __future__ import annotations

from typing import Protocol

from bub.channels.message import ChannelMessage
from loguru import logger

from ..inbound.group import resolve_group_openid
from ..session import QQSessionState
from ..store import QQPlatformStore
from .send_flow import DEFAULT_PASSIVE_REPLIES_PER_MSG_ID
from .send_flow import DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS
from .send_flow import ActiveSender
from .send_flow import normalize_outbound_content
from .send_flow import run_send_flow


class QQGroupOpenAPI(Protocol):
    async def post_group_text_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...

    async def post_group_markdown_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...

    async def post_group_active_text_message(
        self,
        *,
        group_openid: str,
        content: str,
    ) -> dict[str, object]: ...


class QQGroupSendService:
    def __init__(
        self,
        *,
        channel_name: str,
        receive_mode: str,
        state: QQSessionState,
        openapi: QQGroupOpenAPI,
        passive_reply_window_seconds: float = DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS,
        passive_replies_per_msg_id: int = DEFAULT_PASSIVE_REPLIES_PER_MSG_ID,
        active_messages: bool = False,
        platform_store: QQPlatformStore | None = None,
    ) -> None:
        self._channel_name = channel_name
        self._receive_mode = receive_mode
        self._state = state
        self._openapi = openapi
        self._passive_reply_window_seconds = passive_reply_window_seconds
        self._passive_replies_per_msg_id = passive_replies_per_msg_id
        self._active_messages = active_messages
        self._platform_store = platform_store

    async def send(self, message: ChannelMessage) -> dict[str, object] | None:
        content = normalize_outbound_content(message.content or "")
        if not content:
            logger.warning("qq.send skip_empty session_id={}", message.session_id)
            return None

        session_id = message.session_id or ""
        group_openid = resolve_group_openid(
            channel_name=self._channel_name,
            session_id=session_id,
            chat_id=message.chat_id or "",
        )
        if not group_openid:
            logger.warning(
                "qq.send unresolved_group_openid session_id={} chat_id={}",
                message.session_id,
                message.chat_id,
            )
            return None

        async def send_text(
            *, content: str, msg_id: str, msg_seq: int
        ) -> dict[str, object]:
            return await self._openapi.post_group_text_message(
                group_openid=group_openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

        async def send_markdown(
            *, content: str, msg_id: str, msg_seq: int
        ) -> dict[str, object]:
            return await self._openapi.post_group_markdown_message(
                group_openid=group_openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )

        send_active_text: ActiveSender | None = None
        active_messages_allowed: bool | None = None
        if self._active_messages:
            # Active messages are plain text only: markdown in proactive
            # pushes historically requires a registered template.
            async def _send_active_text(*, content: str) -> dict[str, object]:
                return await self._openapi.post_group_active_text_message(
                    group_openid=group_openid,
                    content=content,
                )

            send_active_text = _send_active_text
            if self._platform_store is not None:
                active_messages_allowed = self._platform_store.active_messages_allowed(
                    "group", group_openid
                )

        return await run_send_flow(
            state=self._state,
            receive_mode=self._receive_mode,
            session_id=session_id,
            target_openid=group_openid,
            content=content,
            send_text=send_text,
            send_markdown=send_markdown,
            passive_reply_window_seconds=self._passive_reply_window_seconds,
            passive_replies_per_msg_id=self._passive_replies_per_msg_id,
            send_active_text=send_active_text,
            active_messages_allowed=active_messages_allowed,
        )
