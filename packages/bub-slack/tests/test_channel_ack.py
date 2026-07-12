"""Inbound ack reaction lifecycle (:hourglass: on accept → :white_check_mark: on first reply)."""

from __future__ import annotations

from typing import Any

import pytest
from bub.channels.message import ChannelMessage
from bub_slack.channel import _ACK_EMOJI_IN_PROGRESS, _ACK_PENDING, SlackChannel
from bub_slack.config import SlackSettings

pytestmark = pytest.mark.asyncio


class _RecordingWeb:
    def __init__(self) -> None:
        self.reactions: list[dict[str, Any]] = []

    async def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self.reactions.append({"op": "add", **kwargs})
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, Any]:
        self.reactions.append({"op": "remove", **kwargs})
        return {"ok": True}


def _channel_with_web(on_receive: object = None) -> tuple[SlackChannel, _RecordingWeb]:
    ch = SlackChannel(
        on_receive=on_receive,  # type: ignore[arg-type]
        settings=SlackSettings.model_construct(bot_token="xoxb", app_token="xapp"),
    )
    ch._bot_user_id = "UBOT"
    web = _RecordingWeb()
    ch._web_client = web  # type: ignore[assignment]
    return ch, web


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "user": "U1",
        "text": "<@UBOT> hi",
        "ts": "123.45",
    }
    base.update(overrides)
    return base


async def test_accept_reacts_hourglass() -> None:
    received: list[ChannelMessage] = []

    async def _recv(m: ChannelMessage) -> None:
        received.append(m)

    ch, web = _channel_with_web(_recv)
    await ch._handle_message(_event())

    assert len(received) == 1
    # ack recorded + ⏳ added on the exact inbound (channel, ts)
    assert _ACK_PENDING[received[0].session_id] == ("C1", "123.45")
    assert {
        "op": "add",
        "channel": "C1",
        "timestamp": "123.45",
        "name": _ACK_EMOJI_IN_PROGRESS,
    } in web.reactions


async def test_dropped_message_not_acked() -> None:
    ch, web = _channel_with_web()
    await ch._handle_message(_event(bot_id="B1"))  # bot echo → ignored
    await ch._handle_message(_event(subtype="message_changed"))  # subtype → ignored
    assert web.reactions == []
    assert _ACK_PENDING == {}


class _FailingReactWeb(_RecordingWeb):
    async def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("missing_scope")

    async def reactions_remove(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")


async def test_reaction_failure_does_not_block() -> None:
    """A reaction API failure must never prevent the message from being handled."""
    received: list[ChannelMessage] = []

    async def _recv(m: ChannelMessage) -> None:
        received.append(m)

    ch = SlackChannel(
        on_receive=_recv,
        settings=SlackSettings.model_construct(bot_token="xoxb", app_token="xapp"),
    )
    ch._bot_user_id = "UBOT"
    ch._web_client = _FailingReactWeb()  # type: ignore[assignment]

    await ch._handle_message(_event())  # must not raise
    assert len(received) == 1  # …and the message is still delivered
