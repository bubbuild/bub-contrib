from __future__ import annotations

import asyncio
import contextlib
import uuid
from asyncio import Event
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Literal

import bub
from bub.channels import Channel, ChannelMessage
from bub.channels.message import MediaItem
from bub.channels.contracts import MessageHandler
from loguru import logger
from weixin_bot import IncomingMessage, WeixinBot
from weixin_bot.api import MessageItemType, send_message
from weixin_bot.types import MessageItem, MessageState, MessageType, SendMessageMessage


def get_token_path() -> Path:
    """Return the WeChat token path under Bub's runtime data directory."""
    return bub.home / "wechat_token.json"


@dataclass
class OutgoingMedia:
    media_type: Literal["image", "video", "file"]
    file_path: str

    def to_data_url(self) -> str:
        import mimetypes

        mime_type, _ = mimetypes.guess_type(self.file_path)
        if not mime_type:
            raise ValueError(f"Could not determine MIME type for {self.file_path}")
        with open(self.file_path, "rb") as f:
            data = f.read()
        import base64

        encoded = base64.b64encode(data).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"


@dataclass
class OutgoingMessage:
    text: str
    media: OutgoingMedia | None = None


class WeChatChannel(Channel):
    name = "wechat"

    def __init__(self, on_receive: MessageHandler) -> None:
        self.on_receive = on_receive
        self.bot = WeixinBot(token_path=str(get_token_path()))
        self.bot.on_message(self.process_message)
        self._ongoing_task: asyncio.Task | None = None

    @property
    def needs_debounce(self) -> bool:
        return True

    async def process_message(self, message: IncomingMessage) -> None:
        cm = self._build_message(message)
        await self.on_receive(cm)

    async def send_outgoing(self, chat_id: str, message: OutgoingMessage) -> None:
        items: list[MessageItem] = []
        if message.text:
            items.append(
                {"text_item": {"text": message.text}, "type": MessageItemType.TEXT}
            )
        if message.media:
            if message.media.media_type == "image":
                items.append(
                    {
                        "image_item": {"url": message.media.to_data_url()},
                        "type": MessageItemType.IMAGE,
                    }
                )
            elif message.media.media_type == "video":
                items.append(
                    {
                        "video_item": {"url": message.media.to_data_url()},
                        "type": MessageItemType.VIDEO,
                    }
                )
            elif message.media.media_type == "file":
                items.append(
                    {
                        "file_item": {"url": message.media.to_data_url()},
                        "type": MessageItemType.FILE,
                    }
                )
        context_token = self.bot._context_tokens.get(chat_id)
        if context_token is None:
            raise RuntimeError(
                f"No cached context token for user {chat_id}. Reply to an incoming message first."
            )
        msg = SendMessageMessage(
            from_user_id="",
            to_user_id=chat_id,
            client_id=str(uuid.uuid4()),
            message_type=MessageType.BOT,
            message_state=MessageState.FINISH,
            context_token=context_token,
            item_list=items,
        )
        credentials = await self.bot._ensure_credentials()
        await send_message(self.bot._base_url, credentials.token, msg)

    async def send(self, message: ChannelMessage) -> None:
        await self.bot.send(message.chat_id, message.content)

    @staticmethod
    def _extract_media(item: MessageItem) -> tuple[str | None, MediaItem | None]:
        if text := item.get("text_item"):
            return text["text"], None
        if image := item.get("image_item"):
            media_item = MediaItem(
                type="image", url=image["url"], mime_type="image/jpeg"
            )
            return None, media_item
        return None, None

    def _build_message(self, message: IncomingMessage) -> ChannelMessage:
        session_id = f"{self.name}:{message.user_id}"
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

        cm = ChannelMessage(
            session_id=session_id,
            channel=self.name,
            content=message.text,
            chat_id=message.user_id,
            is_active=True,
            lifespan=lifespan(),
        )
        for item in message.raw["item_list"]:
            if ref_message := item.get("ref_msg"):
                ref_item = ref_message.get("message_item")
                if ref_item:
                    text, media = self._extract_media(ref_item)
                    if text:
                        cm.content += f"\n[引用消息] {text}"
                    if media:
                        cm.media.append(media)
            else:
                _, media = self._extract_media(item)
                if media:
                    cm.media.append(media)
        return cm

    async def start(self, stop_event: Event) -> None:
        self.bot._stopped = False
        self._ongoing_task = asyncio.create_task(self.bot._run_loop())
        logger.info("channel.wechat started")

    async def stop(self) -> None:
        self.bot.stop()
        if self._ongoing_task:
            await self._ongoing_task
        logger.info("channel.wechat stopped")
        self._ongoing_task = None
