from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from asyncio import Event
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bub.channels import Channel, ChannelMessage
from bub.channels.message import MediaItem
from bub.types import MessageHandler
from loguru import logger
from weixin_bot import IncomingMessage, WeixinBot

TOKEN_PATH = Path.home() / ".bub/wechat_token.json"


@dataclass
class WeChatSessionState:
    latest_message_id_by_session: dict[str, str]
    latest_context_token_by_session: dict[str, str]
    latest_timestamp_by_session: dict[str, str]
    send_record_by_session_and_message_id: dict[tuple[str, str], "WeChatSendRecord"]


@dataclass(frozen=True)
class WeChatSendRecord:
    content: str
    content_hash: str


class WeChatDeduper:
    """Bounded recent-message cache for duplicate WeChat deliveries."""

    def __init__(self, size: int = 1024) -> None:
        self._ids: deque[str] = deque(maxlen=size)
        self._id_set: set[str] = set()

    def seen(self, message_id: str) -> bool:
        if message_id in self._id_set:
            return True
        evicted: str | None = None
        if len(self._ids) == self._ids.maxlen:
            evicted = self._ids[0]
        self._ids.append(message_id)
        self._id_set.add(message_id)
        if evicted is not None and evicted not in self._ids:
            self._id_set.discard(evicted)
        return False


class WeChatChannel(Channel):
    name = "wechat"

    def __init__(self, on_receive: MessageHandler) -> None:
        self.on_receive = on_receive
        self.bot = WeixinBot(token_path=str(TOKEN_PATH))
        self.bot.on_message(self.process_message)
        self._ongoing_task: asyncio.Task | None = None
        self._deduper = WeChatDeduper()
        self._state = WeChatSessionState(
            latest_message_id_by_session={},
            latest_context_token_by_session={},
            latest_timestamp_by_session={},
            send_record_by_session_and_message_id={},
        )

    @property
    def needs_debounce(self) -> bool:
        return True

    async def process_message(self, message: IncomingMessage) -> None:
        message_id = self._message_id(message)
        if self._deduper.seen(message_id):
            logger.info("wechat.inbound.duplicate session_id={} message_id={}", self._session_id(message), message_id)
            return

        cm = self._build_message(message, message_id=message_id)
        self._remember_session(message, session_id=cm.session_id, message_id=message_id)
        logger.info("wechat.inbound.accepted session_id={} message_id={}", cm.session_id, message_id)
        await self.on_receive(cm)

    async def send(self, message: ChannelMessage) -> None:
        content = normalize_outbound_content(message.content or "")
        if not content:
            logger.warning("wechat.send skip_empty session_id={}", message.session_id)
            return

        session_id = message.session_id or ""
        chat_id = message.chat_id or ""
        user_id = resolve_user_id(channel_name=self.name, session_id=session_id, chat_id=chat_id)
        if not user_id:
            logger.warning("wechat.send unresolved_user session_id={} chat_id={}", session_id, chat_id)
            return

        message_id = self._state.latest_message_id_by_session.get(session_id)
        context_token = self._state.latest_context_token_by_session.get(session_id)
        if not message_id or not context_token:
            logger.warning("wechat.send missing_context session_id={} chat_id={}", session_id, chat_id)
            return

        content_hash = hash_content(content)
        record = self._state.send_record_by_session_and_message_id.get((session_id, message_id))
        if record is not None:
            if record.content_hash == content_hash:
                logger.info(
                    "wechat.send already_sent session_id={} user_id={} message_id={} content_hash={}",
                    session_id,
                    user_id,
                    message_id,
                    content_hash,
                )
                return
            logger.warning(
                "wechat.send duplicate_reply_blocked session_id={} user_id={} "
                "message_id={} previous_content_hash={} content_hash={}",
                session_id,
                user_id,
                message_id,
                record.content_hash,
                content_hash,
            )
            return

        await self.bot._send_text(user_id, content, context_token)
        self._state.send_record_by_session_and_message_id[(session_id, message_id)] = WeChatSendRecord(
            content=content,
            content_hash=content_hash,
        )
        logger.info(
            "wechat.send success session_id={} user_id={} message_id={} content_hash={}",
            session_id,
            user_id,
            message_id,
            content_hash,
        )

    @staticmethod
    def _extract_media(item: dict[str, Any]) -> tuple[str | None, MediaItem | None]:
        if text := item.get("text_item"):
            return text["text"], None
        if image := item.get("image_item"):
            media_item = MediaItem(type="image", url=image["url"], mime_type="image/jpeg")
            return None, media_item
        return None, None

    def _build_message(self, message: IncomingMessage, *, message_id: str) -> ChannelMessage:
        session_id = self._session_id(message)
        if message.text.startswith(","):
            return ChannelMessage(
                session_id=session_id,
                channel=self.name,
                content=message.text,
                chat_id=message.user_id,
                is_active=True,
                kind="command",
            )

        @contextlib.asynccontextmanager
        async def lifespan() -> AsyncIterator[None]:
            try:
                await self.bot.send_typing(message.user_id)
                yield
            finally:
                await self.bot.stop_typing(message.user_id)

        payload: dict[str, Any] = {
            "message": message.text,
            "message_id": message_id,
            "type": message.type,
            "sender_id": message.user_id,
            "date": message.timestamp.isoformat(),
            "attachments": [],
        }
        media: list[MediaItem] = []
        for item in message.raw["item_list"]:
            if ref_message := item.get("ref_msg"):
                ref_item = ref_message.get("message_item")
                if ref_item:
                    text, media_item = self._extract_media(ref_item)
                    if text:
                        payload["message"] += f"\n[引用消息] {text}"
                    if media_item:
                        media.append(media_item)
            else:
                _, media_item = self._extract_media(item)
                if media_item:
                    media.append(media_item)

        if not media:
            payload.pop("attachments")
        else:
            payload["attachments"] = [
                {
                    "type": item.type,
                    "mime_type": item.mime_type,
                    "url": item.url,
                    "filename": item.filename,
                }
                for item in media
            ]

        return ChannelMessage(
            session_id=session_id,
            channel=self.name,
            content=json.dumps(payload, ensure_ascii=False),
            chat_id=message.user_id,
            is_active=True,
            lifespan=lifespan(),
            media=media,
        )

    def _remember_session(self, message: IncomingMessage, *, session_id: str, message_id: str) -> None:
        self._state.latest_message_id_by_session[session_id] = message_id
        self._state.latest_context_token_by_session[session_id] = message._context_token
        self._state.latest_timestamp_by_session[session_id] = message.timestamp.isoformat()

    @staticmethod
    def _session_id(message: IncomingMessage) -> str:
        return f"wechat:{message.user_id}"

    @staticmethod
    def _message_id(message: IncomingMessage) -> str:
        raw_message_id = message.raw.get("message_id")
        if raw_message_id is not None:
            return str(raw_message_id)
        fingerprint = {
            "user_id": message.user_id,
            "text": message.text,
            "timestamp": message.timestamp.isoformat(),
            "items": message.raw.get("item_list", []),
        }
        return hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    async def start(self, stop_event: Event) -> None:
        del stop_event
        self.bot._stopped = False
        self._ongoing_task = asyncio.create_task(self.bot._run_loop())
        logger.info("channel.wechat started")

    async def stop(self) -> None:
        self.bot.stop()
        if self._ongoing_task:
            await self._ongoing_task
        logger.info("channel.wechat stopped")
        self._ongoing_task = None


def resolve_user_id(*, channel_name: str, session_id: str, chat_id: str) -> str | None:
    if chat_id:
        return chat_id
    prefix = f"{channel_name}:"
    if session_id.startswith(prefix):
        user_id = session_id.removeprefix(prefix).strip()
        return user_id or None
    return None


def normalize_outbound_content(content: str) -> str:
    normalized = content.strip()
    if not normalized:
        return ""
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        return normalized
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message.strip()
    return normalized


def hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
