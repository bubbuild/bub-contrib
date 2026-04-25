"""Discord channel adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, cast

import bub
import discord
from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.types import MessageHandler
from discord.ext import commands
from loguru import logger
from pydantic_settings import SettingsConfigDict


def _message_type(message: discord.Message) -> str:
    if message.content:
        return "text"
    if message.attachments:
        return "attachment"
    if message.stickers:
        return "sticker"
    return "unknown"


@bub.config(name="discord")
class DiscordConfig(bub.Settings):
    """Discord adapter config."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_DISCORD_", env_file=".env", extra="ignore"
    )

    token: str = ""
    allow_users: str | None = None
    allow_channels: str | None = None
    command_prefix: str = "!"
    proxy: str | None = None


class DiscordChannel(Channel):
    """Discord adapter based on discord.py."""

    name = "discord"

    def __init__(self, on_receive: MessageHandler) -> None:
        self._on_receive = on_receive
        self._config = bub.ensure_config(DiscordConfig)
        self._bot: commands.Bot | None = None
        self._latest_message_by_session: dict[str, discord.Message] = {}
        self._allow_users = (
            set(user.strip() for user in self._config.allow_users.split(","))
            if self._config.allow_users
            else set()
        )
        self._allow_channels = (
            set(channel.strip() for channel in self._config.allow_channels.split(","))
            if self._config.allow_channels
            else set()
        )
        self._task: asyncio.Task | None = None

    async def start(self, stop_event: asyncio.Event) -> None:
        self._task = asyncio.create_task(self._main_loop())

    @property
    def needs_debounce(self) -> bool:
        return True

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _main_loop(self) -> None:
        if not self._config.token:
            raise RuntimeError("discord token is empty")

        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True

        proxy = self._config.proxy
        bot = commands.Bot(
            command_prefix=self._config.command_prefix,
            intents=intents,
            help_command=None,
            proxy=proxy,
        )
        self._bot = bot

        @bot.event
        async def on_ready() -> None:
            logger.info(
                "discord.ready user={} id={}",
                str(bot.user),
                bot.user.id if bot.user else "<unknown>",
            )

        @bot.event
        async def on_message(message: discord.Message) -> None:
            await bot.process_commands(message)
            await self._on_message(message)

        logger.info(
            "discord.start allow_from_count={} allow_channels_count={} proxy_enabled={}",
            len(self._allow_users),
            len(self._allow_channels),
            bool(proxy),
        )
        try:
            async with bot:
                await bot.start(self._config.token)
        finally:
            self._bot = None
            logger.info("discord.stopped")

    def _build_message(self, message: discord.Message) -> ChannelMessage:
        channel_id = str(message.channel.id)
        session_id = f"{self.name}:{channel_id}"
        content, media = self._parse_message(message)

        prefix = f"{self._config.command_prefix}bub "
        if content.startswith(prefix):
            content = content[len(prefix) :]

        if content.strip().startswith(","):
            return ChannelMessage(
                content=content.strip(),
                session_id=session_id,
                channel=self.name,
                chat_id=channel_id,
                kind="command",
            )

        metadata: dict[str, Any] = {
            "message_id": message.id,
            "type": _message_type(message),
            "username": message.author.name,
            "full_name": getattr(message.author, "display_name", message.author.name),
            "sender_id": str(message.author.id),
            "date": message.created_at.timestamp() if message.created_at else None,
            "channel_id": str(message.channel.id),
            "guild_id": str(message.guild.id) if message.guild else None,
        }

        if media:
            metadata["media"] = media

        reply_meta = self._extract_reply_metadata(message)
        if reply_meta:
            metadata["reply_to_message"] = reply_meta

        metadata_json = json.dumps(
            {"message": content, "channel_id": channel_id, **exclude_none(metadata)},
            ensure_ascii=False,
        )
        return ChannelMessage(
            content=metadata_json,
            session_id=session_id,
            channel=self.name,
            chat_id=channel_id,
            is_active=self.is_mentioned(message),
            lifespan=message.channel.typing(),
        )

    async def send(self, message: ChannelMessage) -> None:
        channel = await self._resolve_channel(message.session_id)
        if channel is None:
            logger.warning(
                "discord.outbound unresolved channel session_id={}", message.session_id
            )
            return

        source = self._latest_message_by_session.get(message.session_id)
        reference = (
            source.to_reference(fail_if_not_exists=False)
            if source is not None
            else None
        )
        for chunk in self._chunk_message(message.content):
            kwargs: dict[str, Any] = {"content": chunk}
            if reference is not None:
                kwargs["reference"] = reference
                kwargs["mention_author"] = False
            await channel.send(**kwargs)

    async def _on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        channel_id = str(message.channel.id)
        if (
            self._config.allow_channels
            and channel_id not in self._config.allow_channels
        ):
            return

        if not message.content.strip():
            return

        sender_tokens = {str(message.author.id), message.author.name}
        if getattr(message.author, "global_name", None):
            sender_tokens.add(cast(str, message.author.global_name))
        if self._allow_users and sender_tokens.isdisjoint(self._allow_users):
            logger.warning(
                "discord.inbound.denied channel_id={} sender_id={} reason=allow_from",
                message.channel.id,
                message.author.id,
            )
            return

        payload = self._build_message(message)
        self._latest_message_by_session[payload.session_id] = message
        logger.info(
            "discord.inbound channel_id={} sender_id={} username={} content={}",
            message.channel.id,
            message.author.id,
            message.author.name,
            payload.content[:100],
        )
        await self._on_receive(payload)

    async def _resolve_channel(self, session_id: str) -> discord.abc.Messageable | None:
        if self._bot is None:
            return None
        channel_id = int(session_id.split(":", 1)[1])
        channel = self._bot.get_channel(channel_id)
        if channel is not None:
            return channel  # type: ignore[return-value]
        with contextlib.suppress(Exception):
            fetched = await self._bot.fetch_channel(channel_id)
            if isinstance(fetched, discord.abc.Messageable):
                return fetched
        return None

    def is_mentioned(self, message: discord.Message) -> bool:
        if (
            isinstance(message.channel, discord.DMChannel)
            or "bub" in message.content.lower()
            or self._is_bub_scoped_thread(message)
            or message.content.startswith(f"{self._config.command_prefix}bub")
        ):
            return True

        bot_user = self._bot.user if self._bot is not None else None
        if bot_user is None:
            return False
        if bot_user in message.mentions:
            return True

        ref = message.reference
        if ref is None:
            return False
        resolved = ref.resolved
        return bool(
            isinstance(resolved, discord.Message)
            and resolved.author
            and resolved.author.id == bot_user.id
        )

    @staticmethod
    def _is_bub_scoped_thread(message: discord.Message) -> bool:
        channel = message.channel
        thread_name = getattr(channel, "name", None)
        if not isinstance(thread_name, str):
            return False
        is_thread = (
            isinstance(channel, discord.Thread)
            or getattr(channel, "parent", None) is not None
        )
        return is_thread and thread_name.lower().startswith("bub")

    @staticmethod
    def _parse_message(message: discord.Message) -> tuple[str, dict[str, Any] | None]:
        if message.content:
            return message.content, None

        if message.attachments:
            attachment_lines: list[str] = []
            attachment_meta: list[dict[str, Any]] = []
            for att in message.attachments:
                attachment_lines.append(f"[Attachment: {att.filename}]")
                attachment_meta.append(
                    exclude_none(
                        {
                            "id": str(att.id),
                            "filename": att.filename,
                            "content_type": att.content_type,
                            "size": att.size,
                            "url": att.url,
                        }
                    )
                )
            return "\n".join(attachment_lines), {"attachments": attachment_meta}

        if message.stickers:
            lines = [f"[Sticker: {sticker.name}]" for sticker in message.stickers]
            meta = [
                {"id": str(sticker.id), "name": sticker.name}
                for sticker in message.stickers
            ]
            return "\n".join(lines), {"stickers": meta}

        return "[Unknown message type]", None

    @staticmethod
    def _extract_reply_metadata(message: discord.Message) -> dict[str, Any] | None:
        ref = message.reference
        if ref is None:
            return None
        resolved = ref.resolved
        if not isinstance(resolved, discord.Message):
            return None
        return exclude_none(
            {
                "message_id": str(resolved.id),
                "from_user_id": str(resolved.author.id),
                "from_username": resolved.author.name,
                "from_is_bot": resolved.author.bot,
                "text": (resolved.content or "")[:100],
            }
        )

    @staticmethod
    def _chunk_message(text: str, *, limit: int = 2000) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip("\n")
        return [chunk for chunk in chunks if chunk]


def exclude_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}
