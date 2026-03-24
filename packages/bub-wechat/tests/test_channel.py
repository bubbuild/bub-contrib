from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from bub.channels.message import ChannelMessage
from weixin_bot import IncomingMessage

from bub_wechat.channel import WeChatChannel


@dataclass
class BotStub:
    sent: list[tuple[str, str, str]]

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


def test_channel_send_uses_latest_message_context() -> None:
    async def _run() -> None:
        channel = WeChatChannel(lambda message: None)
        bot = BotStub([])
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

        assert bot.sent == [("user-1", "hello", "ctx-1001")]

    asyncio.run(_run())


def test_c2c_inbound_defaults_outbound_to_wechat_channel() -> None:
    channel = WeChatChannel(lambda message: None)
    channel_message = channel._build_message(_incoming_message(1001), message_id="1001")

    assert channel_message.output_channel != "null"
