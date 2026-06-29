from __future__ import annotations

from typing import Any

import pytest
from bub.channels.message import ChannelMessage
from bub_slack.channel import _ACK_PENDING, _SLACK_CHUNK_SIZE, SlackChannel
from bub_slack.config import SlackSettings

pytestmark = pytest.mark.asyncio


class _FakeWebClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self.reactions.append({"op": "add", **kwargs})
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, Any]:
        self.reactions.append({"op": "remove", **kwargs})
        return {"ok": True}


def _channel_with_web() -> tuple[SlackChannel, _FakeWebClient]:
    ch = SlackChannel(
        on_receive=None,  # type: ignore[arg-type]  # not exercised by send()
        settings=SlackSettings.model_construct(bot_token="xoxb", app_token="xapp"),
    )
    web = _FakeWebClient()
    ch._web_client = web  # type: ignore[assignment]
    return ch, web


def _msg(content: str, thread_ts: str = "") -> ChannelMessage:
    return ChannelMessage(
        session_id="slack:C1",
        channel="slack",
        chat_id="C1",
        content=content,
        context={"thread_ts": thread_ts},
    )


async def test_send_plain() -> None:
    ch, web = _channel_with_web()
    await ch.send(_msg("hello"))
    assert len(web.posts) == 1
    assert web.posts[0]["text"] == "hello"
    assert web.posts[0]["channel"] == "C1"
    assert "thread_ts" not in web.posts[0]


async def test_send_unwraps_json() -> None:
    ch, web = _channel_with_web()
    await ch.send(_msg('{"message": "wrapped"}'))
    assert len(web.posts) == 1
    assert web.posts[0]["text"] == "wrapped"


async def test_send_chunks_long_text() -> None:
    ch, web = _channel_with_web()
    text = "y" * (_SLACK_CHUNK_SIZE + 1100)
    await ch.send(_msg(text))
    assert len(web.posts) == 2
    assert len(web.posts[0]["text"]) == _SLACK_CHUNK_SIZE
    assert len(web.posts[1]["text"]) == 1100


async def test_send_thread_ts_forwarded_to_all_chunks() -> None:
    ch, web = _channel_with_web()
    text = "y" * (_SLACK_CHUNK_SIZE + 1)
    await ch.send(_msg(text, thread_ts="12345.67"))
    assert len(web.posts) == 2
    assert all(p["thread_ts"] == "12345.67" for p in web.posts)


async def test_send_recovers_thread_ts_from_session() -> None:
    """Regression: Bub's render_outbound drops inbound context, so the thread_ts
    must be recoverable from the thread-scoped session_id to stay in-thread."""
    ch, web = _channel_with_web()
    msg = ChannelMessage(
        session_id="slack:C1:12345.67",  # thread-scoped, no context thread_ts
        channel="slack",
        chat_id="C1",
        content="hello",
        context={},
    )
    await ch.send(msg)
    assert len(web.posts) == 1
    assert web.posts[0]["thread_ts"] == "12345.67"


async def test_send_context_thread_ts_takes_precedence() -> None:
    """When context carries a thread_ts, it wins over the session-derived one."""
    ch, web = _channel_with_web()
    msg = ChannelMessage(
        session_id="slack:C1:999.9",
        channel="slack",
        chat_id="C1",
        content="hello",
        context={"thread_ts": "12345.67"},
    )
    await ch.send(msg)
    assert web.posts[0]["thread_ts"] == "12345.67"


async def test_send_records_active_thread() -> None:
    """Posting into a thread registers it so plain replies there are addressed."""
    ch, web = _channel_with_web()
    msg = ChannelMessage(
        session_id="slack:C1:12345.67",
        channel="slack",
        chat_id="C1",
        content="hello",
        context={},
    )
    await ch.send(msg)
    assert "12345.67" in ch._active_threads


async def test_send_dm_session_has_no_thread_ts() -> None:
    """A DM (channel-scoped) session id yields no thread_ts — posts to DM root."""
    ch, web = _channel_with_web()
    msg = ChannelMessage(
        session_id="slack:CDM",
        channel="slack",
        chat_id="CDM",
        content="hello",
        context={},
    )
    await ch.send(msg)
    assert len(web.posts) == 1
    assert "thread_ts" not in web.posts[0]


async def test_send_no_web_client_is_noop() -> None:
    ch = SlackChannel(
        on_receive=None,  # type: ignore[arg-type]
        settings=SlackSettings.model_construct(bot_token="xoxb", app_token="xapp"),
    )
    # _web_client left as None
    await ch.send(_msg("hello"))  # must not raise


async def test_send_empty_content_skipped() -> None:
    ch, web = _channel_with_web()
    await ch.send(_msg("   "))
    assert web.posts == []


async def test_send_swaps_ack_reaction() -> None:
    """First reply swaps the inbound ack ⏳ → ✅ (one-shot; later sends don't re-react)."""
    ch, web = _channel_with_web()
    _ACK_PENDING["slack:C1"] = ("C1", "9.9")
    await ch.send(_msg("done"))
    assert {"op": "remove", "channel": "C1", "timestamp": "9.9", "name": "hourglass"} in web.reactions
    assert {
        "op": "add",
        "channel": "C1",
        "timestamp": "9.9",
        "name": "white_check_mark",
    } in web.reactions
    # consumed — a second send for the same session adds no further reactions
    web.reactions.clear()
    await ch.send(_msg("more"))
    assert web.reactions == []
