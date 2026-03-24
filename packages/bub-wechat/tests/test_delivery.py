from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from bub.channels.message import ChannelMessage
from weixin_bot import IncomingMessage

from bub_wechat.channel import WeChatChannel


class BotStub:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def _send_text(self, user_id: str, text: str, context_token: str) -> None:
        self.sent.append((user_id, text, context_token))

    async def send_typing(self, user_id: str) -> None:
        del user_id

    async def stop_typing(self, user_id: str) -> None:
        del user_id

    def on_message(self, handler) -> None:
        del handler

    def stop(self) -> None:
        return None


def _incoming_message(message_id: int, text: str = "hello") -> IncomingMessage:
    timestamp = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)
    raw = {
        "message_id": message_id,
        "from_user_id": "user-1",
        "to_user_id": "bot-1",
        "client_id": f"client-{message_id}",
        "create_time_ms": int(timestamp.timestamp() * 1000),
        "message_type": "user",
        "message_state": "finish",
        "context_token": f"ctx-{message_id}",
        "item_list": [{"text_item": {"text": text}}],
    }
    return IncomingMessage(
        user_id="user-1",
        text=text,
        type="text",
        raw=raw,
        _context_token=f"ctx-{message_id}",
        timestamp=timestamp,
    )


def test_process_message_dedupes_repeated_messages() -> None:
    async def _run() -> None:
        received: list[str] = []

        async def on_receive(message) -> None:
            received.append(message.session_id)

        channel = WeChatChannel(on_receive)
        channel.bot = BotStub()  # type: ignore[assignment]

        message = _incoming_message(1001)
        await channel.process_message(message)
        await channel.process_message(message)

        assert received == ["wechat:user-1"]

    asyncio.run(_run())


def test_send_same_content_is_idempotent() -> None:
    async def _run() -> None:
        channel = WeChatChannel(lambda message: None)
        bot = BotStub()
        channel.bot = bot  # type: ignore[assignment]
        channel._state.latest_message_id_by_session["wechat:user-1"] = "1001"
        channel._state.latest_context_token_by_session["wechat:user-1"] = "ctx-1001"

        message = ChannelMessage(
            session_id="wechat:user-1",
            content="hello",
            channel="wechat",
            chat_id="user-1",
        )
        await channel.send(message)
        await channel.send(message)

        assert bot.sent == [("user-1", "hello", "ctx-1001")]

    asyncio.run(_run())


def test_send_different_content_for_same_message_is_blocked() -> None:
    async def _run() -> None:
        channel = WeChatChannel(lambda message: None)
        bot = BotStub()
        channel.bot = bot  # type: ignore[assignment]
        channel._state.latest_message_id_by_session["wechat:user-1"] = "1001"
        channel._state.latest_context_token_by_session["wechat:user-1"] = "ctx-1001"

        await channel.send(
            ChannelMessage(
                session_id="wechat:user-1",
                content="hello",
                channel="wechat",
                chat_id="user-1",
            )
        )
        await channel.send(
            ChannelMessage(
                session_id="wechat:user-1",
                content="different",
                channel="wechat",
                chat_id="user-1",
            )
        )

        assert bot.sent == [("user-1", "hello", "ctx-1001")]

    asyncio.run(_run())
