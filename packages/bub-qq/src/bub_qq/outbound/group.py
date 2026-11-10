from __future__ import annotations

from pathlib import Path
from typing import Protocol

from bub.channels.message import ChannelMessage
from loguru import logger

from ..inbound.group import resolve_group_openid
from ..session import QQSessionState
from ..store import QQPlatformStore
from .media import MediaDownloader
from .media import apply_at_user_tags
from .media import at_user_ids_from_message
from .media import file_info_from_upload
from .media import keyboard_call_kwargs
from .media import keyboard_from_message
from .media import materialize_media_file
from .media import media_from_message
from .media import outbound_dedupe_content
from .media import reply_to_from_message
from .upload import upload_local_file
from .send_flow import GROUP_PASSIVE_REPLIES_PER_MSG_ID
from .send_flow import GROUP_PASSIVE_REPLY_WINDOW_SECONDS
from .send_flow import ActiveSender
from .send_flow import is_no_reply
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

    async def post_group_file(
        self,
        *,
        group_openid: str,
        file_type: int,
        url: str,
        file_name: str | None = None,
    ) -> dict[str, object]: ...

    async def post_group_media_message(
        self,
        *,
        group_openid: str,
        file_info: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...


class QQGroupSendService:
    def __init__(
        self,
        *,
        channel_name: str,
        receive_mode: str,
        state: QQSessionState,
        openapi: QQGroupOpenAPI,
        passive_reply_window_seconds: float = GROUP_PASSIVE_REPLY_WINDOW_SECONDS,
        passive_replies_per_msg_id: int = GROUP_PASSIVE_REPLIES_PER_MSG_ID,
        active_messages: bool = False,
        platform_store: QQPlatformStore | None = None,
        workspace: Path | None = None,
        download_media: MediaDownloader | None = None,
    ) -> None:
        self._channel_name = channel_name
        self._receive_mode = receive_mode
        self._state = state
        self._openapi = openapi
        self._passive_reply_window_seconds = passive_reply_window_seconds
        self._passive_replies_per_msg_id = passive_replies_per_msg_id
        self._active_messages = active_messages
        self._platform_store = platform_store
        self._workspace = workspace if workspace is not None else Path.cwd()
        self._download_media = download_media

    async def send(self, message: ChannelMessage) -> dict[str, object] | None:
        at_user_ids = at_user_ids_from_message(message)
        content = apply_at_user_tags(
            normalize_outbound_content(message.content or ""),
            at_user_ids,
        )
        media = media_from_message(message)
        keyboard = keyboard_from_message(message)
        if not content and media is None and keyboard is None:
            logger.warning("qq.send skip_empty session_id={}", message.session_id)
            return None
        if content and is_no_reply(content):
            logger.info("qq.send skip_no_reply session_id={}", message.session_id)
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
                **keyboard_call_kwargs(keyboard),
            )

        async def send_markdown(
            *, content: str, msg_id: str, msg_seq: int
        ) -> dict[str, object]:
            return await self._openapi.post_group_markdown_message(
                group_openid=group_openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
                **keyboard_call_kwargs(keyboard),
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

        send_media = None
        if media is not None:

            async def _send_media(
                *, msg_id: str, msg_seq: int
            ) -> dict[str, object]:
                local = await materialize_media_file(
                    media,
                    workspace=self._workspace,
                    download=self._download_media,
                )
                uploaded = await upload_local_file(
                    self._openapi,  # type: ignore[arg-type]
                    scope="group",
                    openid=group_openid,
                    path=Path(local.local_path or ""),
                    file_type=local.file_type,
                    file_name=local.file_name,
                )
                return await self._openapi.post_group_media_message(
                    group_openid=group_openid,
                    file_info=file_info_from_upload(uploaded),
                    msg_id=msg_id,
                    msg_seq=msg_seq,
                    **keyboard_call_kwargs(keyboard),
                )

            send_media = _send_media

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
            send_media=send_media,
            force_markdown=keyboard is not None or bool(at_user_ids),
            dedupe_content=outbound_dedupe_content(content, media, keyboard),
            reply_to=reply_to_from_message(message),
        )
