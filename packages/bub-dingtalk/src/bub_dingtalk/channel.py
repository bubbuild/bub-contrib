"""DingTalk channel adapter using Stream Mode."""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any

import bub
from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.types import MessageHandler
from dingtalk_stream import (
    AckMessage,
    CallbackHandler,
    CallbackMessage,
    Credential,
    DingTalkStreamClient,
)
from dingtalk_stream.chatbot import ChatbotMessage
from loguru import logger
from pydantic_settings import SettingsConfigDict


@bub.config(name="dingtalk")
class DingTalkConfig(bub.Settings):
    """DingTalk channel config."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_DINGTALK_",
        env_file=".env",
        extra="ignore",
    )

    client_id: str = ""
    client_secret: str = ""
    allow_users: str = ""  # Comma-separated staff_ids, or "*" for all


def _parse_allow_users(value: str) -> set[str]:
    if not value or not value.strip():
        return set()
    v = value.strip()
    if v == "*":
        return {"*"}
    return {u.strip() for u in v.split(",") if u.strip()}


# Markdown image syntax: ![alt](target). We only act on local paths.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

_URL_PREFIXES = ("http://", "https://", "data:")

_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _mime_for_path(path: Path) -> str:
    return _EXT_TO_MIME.get(path.suffix.lower(), "image/png")


def _extract_local_images(content: str) -> tuple[str, list[tuple[str, Path]]]:
    """Pull local-file image references out of markdown text.

    Returns ``(stripped_content, images)`` where ``images`` is a list of
    ``(alt_text, absolute_path)`` for each local file that exists. Markdown
    image references pointing at http/https/data URLs, or at non-existent
    local paths, are left intact in ``stripped_content``.
    """
    images: list[tuple[str, Path]] = []

    def _replace(match: re.Match[str]) -> str:
        alt = match.group(1)
        raw = match.group(2).strip()
        if raw.startswith(_URL_PREFIXES):
            return match.group(0)
        path_str = raw[7:] if raw.startswith("file://") else raw
        candidate = Path(path_str).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if not candidate.is_file():
            logger.warning("DingTalk image not found, leaving markdown: {}", candidate)
            return match.group(0)
        images.append((alt, candidate))
        return ""

    stripped = _MD_IMAGE_RE.sub(_replace, content)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped, images


class DingTalkCallbackHandler(CallbackHandler):
    """DingTalk Stream callback handler; forwards messages to Bub."""

    def __init__(self, channel: DingTalkChannel) -> None:
        super().__init__()
        self.channel = channel

    async def process(self, message: CallbackMessage) -> tuple[int, str]:
        """Process incoming stream message."""
        try:
            chatbot_msg = ChatbotMessage.from_dict(message.data)
            content = ""
            if chatbot_msg.text:
                content = (chatbot_msg.text.content or "").strip()
            if not content:
                content = message.data.get("text", {}).get("content", "").strip()

            if not content:
                logger.warning(
                    "DingTalk: empty or unsupported message type: {}",
                    getattr(chatbot_msg, "message_type", "?"),
                )
                return AckMessage.STATUS_OK, "OK"

            sender_id = chatbot_msg.sender_staff_id or chatbot_msg.sender_id or ""
            sender_name = chatbot_msg.sender_nick or "Unknown"
            conversation_type = message.data.get("conversationType")
            conversation_id = message.data.get("conversationId") or message.data.get(
                "openConversationId"
            )

            logger.info(
                "DingTalk inbound from {} ({}): {}",
                sender_name,
                sender_id,
                content[:80],
            )

            task = asyncio.create_task(
                self.channel._on_message(
                    content=content,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    conversation_type=conversation_type,
                    conversation_id=conversation_id,
                )
            )
            self.channel._background_tasks.add(task)
            task.add_done_callback(self.channel._background_tasks.discard)

        except Exception as e:
            logger.error("DingTalk process error: {}", e)
            return AckMessage.STATUS_OK, "Error"
        else:
            return AckMessage.STATUS_OK, "OK"


class DingTalkChannel(Channel):
    """DingTalk channel using Stream Mode (WebSocket receive, HTTP send)."""

    name = "dingtalk"

    def __init__(self, on_receive: MessageHandler) -> None:
        self._on_receive = on_receive
        self._config = bub.ensure_config(DingTalkConfig)
        self._allow_users = _parse_allow_users(self._config.allow_users)
        self._client: Any = None
        self._background_tasks: set[asyncio.Task] = set()
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._stream_task: asyncio.Task | None = None

    def _is_allowed(self, sender_id: str) -> bool:
        if not self._allow_users:
            return False
        if "*" in self._allow_users:
            return True
        return str(sender_id) in self._allow_users

    async def start(self, stop_event: asyncio.Event) -> None:
        """Start DingTalk Stream client."""
        self._stop_event = stop_event
        if not self._config.client_id or not self._config.client_secret:
            logger.error("DingTalk client_id/client_secret not configured")
            return

        self._main_loop = asyncio.get_running_loop()

        credential = Credential(self._config.client_id, self._config.client_secret)
        self._client = DingTalkStreamClient(credential)
        handler = DingTalkCallbackHandler(self)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, handler)

        logger.info("DingTalk channel starting (Stream Mode)")

        async def _run_stream() -> None:
            while not (self._stop_event and self._stop_event.is_set()):
                try:
                    await self._client.start()
                except Exception as e:
                    logger.warning("DingTalk stream error: {}", e)
                if self._stop_event and self._stop_event.is_set():
                    break
                logger.info("DingTalk reconnecting in 5s...")
                await asyncio.sleep(5)

        self._stream_task = asyncio.create_task(_run_stream())

    async def stop(self) -> None:
        """Stop DingTalk channel."""
        if self._stop_event:
            self._stop_event.set()
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()
        if self._stream_task:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        self._client = None
        logger.info("DingTalk channel stopped")

    async def send(self, message: ChannelMessage) -> None:
        """Send message to DingTalk via skill.

        Detects local image references written as ``![alt](/abs/path.png)``
        in the content. The text is sent first (with the markdown stripped),
        then each local image is uploaded and sent as a ``sampleImageMsg``
        in the order it appeared.
        """
        raw_content = message.content or ""
        content, images = _extract_local_images(raw_content)
        logger.info(
            "DingTalk send: session_id={} chat_id={} content_len={} images={}",
            message.session_id,
            message.chat_id,
            len(content),
            len(images),
        )

        chat_id = message.chat_id or ""
        if not chat_id and message.session_id:
            _, _, chat_id = message.session_id.partition(":")
        if not chat_id:
            logger.warning(
                "DingTalk send: no chat_id session_id={}", message.session_id
            )
            return

        if not content and not images:
            logger.warning(
                "DingTalk send: skipping empty content session_id={}",
                message.session_id,
            )
            return

        from skills.dingtalk.scripts.dingtalk_send import (
            send_image_message,
            send_message,
            upload_media,
        )

        if content:
            logger.info(
                "DingTalk send: text chat_id={} content_len={}", chat_id, len(content)
            )
            try:
                await asyncio.to_thread(
                    send_message,
                    self._config.client_id,
                    self._config.client_secret,
                    chat_id,
                    content,
                    title="Bub Reply",
                )
            except Exception as e:
                logger.error(
                    "DingTalk send text failed chat_id={} error={}", chat_id, e
                )

        for alt, image_path in images:
            logger.info(
                "DingTalk send: image chat_id={} path={} alt={}",
                chat_id,
                image_path,
                alt,
            )
            try:
                file_bytes = image_path.read_bytes()
                media_id = await asyncio.to_thread(
                    upload_media,
                    self._config.client_id,
                    self._config.client_secret,
                    file_bytes,
                    image_path.name,
                    _mime_for_path(image_path),
                )
                await asyncio.to_thread(
                    send_image_message,
                    self._config.client_id,
                    self._config.client_secret,
                    chat_id,
                    media_id,
                )
                logger.info(
                    "DingTalk send: image ok chat_id={} path={}", chat_id, image_path
                )
            except Exception as e:
                logger.error(
                    "DingTalk send image failed chat_id={} path={} error={}",
                    chat_id,
                    image_path,
                    e,
                )

    async def _on_message(
        self,
        content: str,
        sender_id: str,
        sender_name: str,
        conversation_type: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """Handle incoming message from callback handler."""
        if not self._is_allowed(sender_id):
            logger.warning("DingTalk inbound denied: sender_id={}", sender_id)
            return

        is_group = conversation_type == "2" and conversation_id
        chat_id = f"group:{conversation_id}" if is_group else sender_id
        session_id = f"{self.name}:{chat_id}"

        is_command = content.strip().startswith(",")
        channel_msg = ChannelMessage(
            session_id=session_id,
            content=content,
            channel=self.name,
            chat_id=chat_id,
            kind="command" if is_command else "normal",
            is_active=True,
        )
        logger.debug(
            "DingTalk inbound session_id={} content={}", session_id, content[:50]
        )
        await self._on_receive(channel_msg)
