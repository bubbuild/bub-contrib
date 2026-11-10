from __future__ import annotations

import asyncio
import base64
import contextlib
import string
import uuid
from asyncio import Event
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Literal
from urllib.parse import quote

import bub
from bub.channels import Channel, ChannelMessage
from bub.channels.message import MediaItem
from bub.channels.contracts import MessageHandler
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from loguru import logger
from weixin_bot import IncomingMessage, WeixinBot
from weixin_bot.api import MessageItemType, send_message
from weixin_bot.types import (
    ImageItem,
    MessageItem,
    MessageState,
    MessageType,
    SendMessageMessage,
)

CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DOWNLOAD_TIMEOUT_SECONDS = 30
IMAGE_HOLD_SECONDS = 8.0


def get_token_path() -> Path:
    """Return the WeChat token path under Bub's runtime data directory."""
    return bub.home / "wechat_token.json"


def _parse_aes_key(image: ImageItem) -> bytes | None:
    """Return the 16-byte AES key of an image item, or None if it has none."""
    if hex_key := image.get("aeskey"):
        return bytes.fromhex(hex_key)
    encoded = image.get("media", {}).get("aes_key")
    if not encoded:
        return None
    decoded = base64.b64decode(encoded)
    if len(decoded) == 32 and all(chr(b) in string.hexdigits for b in decoded):
        return bytes.fromhex(decoded.decode())
    return decoded


def _decrypt_aes_ecb(data: bytes, key: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(decryptor.update(data) + decryptor.finalize()) + unpadder.finalize()


def _sniff_image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def download_image(image: ImageItem) -> MediaItem:
    """Download an incoming image from the WeChat CDN and decrypt it."""
    import aiohttp

    param = image.get("media", {}).get("encrypt_query_param")
    if not param:
        raise ValueError("image item has no encrypt_query_param")
    url = f"{CDN_BASE_URL}/download?encrypted_query_param={quote(param, safe='')}"
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            data = await response.read()
    if key := _parse_aes_key(image):
        data = _decrypt_aes_ecb(data, key)

    async def fetch() -> bytes:
        return data

    return MediaItem(type="image", mime_type=_sniff_image_mime(data), data_fetcher=fetch)


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
        self._held: dict[str, tuple[ChannelMessage, asyncio.Task]] = {}

    @property
    def needs_debounce(self) -> bool:
        return True

    async def process_message(self, message: IncomingMessage) -> None:
        cm = await self._build_message(message)
        chat_id = message.user_id
        if held := self._held.pop(chat_id, None):
            held_cm, task = held
            task.cancel()
            if cm.kind == "command":
                await self.on_receive(held_cm)
            else:
                cm.media = held_cm.media + cm.media
                cm.content = f"{held_cm.content}\n{cm.content}"
        if self._is_image_only(message) and cm.kind != "command":
            task = asyncio.create_task(self._release_later(chat_id, cm))
            self._held[chat_id] = (cm, task)
            return
        await self.on_receive(cm)

    @staticmethod
    def _is_image_only(message: IncomingMessage) -> bool:
        items = message.raw["item_list"]
        return bool(items) and all(item["type"] == MessageItemType.IMAGE for item in items)

    async def _release_later(self, chat_id: str, cm: ChannelMessage) -> None:
        await asyncio.sleep(IMAGE_HOLD_SECONDS)
        self._held.pop(chat_id, None)
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
    async def _extract_media(item: MessageItem) -> tuple[str | None, MediaItem | None]:
        if text := item.get("text_item"):
            return text["text"], None
        if image := item.get("image_item"):
            try:
                return None, await download_image(image)
            except Exception as exc:
                logger.warning(
                    "channel.wechat image download failed: {!r} keys={} media_keys={}",
                    exc,
                    sorted(image),
                    sorted(image.get("media", {})),
                )
                return "[图片下载失败]", None
        return None, None

    async def _build_message(self, message: IncomingMessage) -> ChannelMessage:
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
                    text, media = await self._extract_media(ref_item)
                    if text:
                        cm.content += f"\n[引用消息] {text}"
                    if media:
                        cm.media.append(media)
            else:
                text, media = await self._extract_media(item)
                if media:
                    cm.media.append(media)
                elif text and "image_item" in item:
                    cm.content += f"\n{text}"
        return cm

    async def start(self, stop_event: Event) -> None:
        self.bot._stopped = False
        self._ongoing_task = asyncio.create_task(self.bot._run_loop())
        logger.info("channel.wechat started")

    async def stop(self) -> None:
        self.bot.stop()
        for _, task in self._held.values():
            task.cancel()
        self._held.clear()
        if self._ongoing_task:
            await self._ongoing_task
        logger.info("channel.wechat stopped")
        self._ongoing_task = None
