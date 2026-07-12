from __future__ import annotations

from typing import Any

import pytest
from bub.channels.message import ChannelMessage
from bub_slack.channel import SlackChannel
from bub_slack.config import SlackSettings

pytestmark = pytest.mark.asyncio


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "user": "U1",
        "text": "hi",
    }
    base.update(overrides)
    return base


async def test_dm_is_always_addressed(channel, captured) -> None:
    await channel._handle_message(_event(channel_type="im", channel="C1", text="hi"))
    assert len(captured) == 1
    assert captured[0].session_id == "slack:C1"
    assert captured[0].content == "hi"
    assert captured[0].context["thread_ts"] == ""


async def test_channel_requires_mention(channel, captured) -> None:
    await channel._handle_message(_event(text="<@UBOT> hello"))
    assert len(captured) == 1
    assert captured[0].content == "hello"  # mention stripped

    captured.clear()
    await channel._handle_message(_event(text="hello"))  # no mention
    assert captured == []


async def test_subtype_ignored(channel, captured) -> None:
    await channel._handle_message(_event(subtype="message_changed"))
    assert captured == []


async def test_bot_echo_ignored(channel, captured) -> None:
    await channel._handle_message(_event(bot_id="B1", text="<@UBOT> hi"))
    assert captured == []


async def test_self_echo_ignored(channel, captured) -> None:
    await channel._handle_message(_event(user="UBOT", text="<@UBOT> hi"))
    assert captured == []


async def test_allow_channels_restriction(on_receive, captured) -> None:
    restricted = SlackChannel(
        on_receive,
        settings=SlackSettings.model_construct(
            bot_token="xoxb", app_token="xapp", allow_channels="C1,C2", allow_users=None
        ),
    )
    restricted._bot_user_id = "UBOT"
    await restricted._handle_message(_event(channel="C9", text="<@UBOT> hi"))
    assert captured == []  # C9 not in allow list → dropped


async def test_allow_channels_does_not_block_dms(on_receive, captured) -> None:
    """Regression: allow_channels constrains shared channels only — DMs must
    still be accepted, otherwise locking the bot to specific channels would
    silently drop every DM."""
    restricted = SlackChannel(
        on_receive,
        settings=SlackSettings.model_construct(
            bot_token="xoxb", app_token="xapp", allow_channels="C1,C2", allow_users=None
        ),
    )
    restricted._bot_user_id = "UBOT"
    await restricted._handle_message(
        _event(channel_type="im", channel="CDM", text="hi")
    )
    assert len(captured) == 1
    assert captured[0].content == "hi"


async def test_allow_users_restriction(on_receive, captured) -> None:
    restricted = SlackChannel(
        on_receive,
        settings=SlackSettings.model_construct(
            bot_token="xoxb", app_token="xapp", allow_channels=None, allow_users="UA,UB"
        ),
    )
    restricted._bot_user_id = "UBOT"
    # user UZ not allowed, but channel is a DM so it's "addressed"; user filter drops it
    await restricted._handle_message(
        _event(channel_type="im", channel="CDM", user="UZ", text="hi")
    )
    assert captured == []


async def test_thread_ts_carried(channel, captured) -> None:
    await channel._handle_message(_event(channel_type="im", thread_ts="12345.67"))
    assert len(captured) == 1
    assert captured[0].context["thread_ts"] == "12345.67"


# ---------------------------------------------------------------------------
# Thread-aware session scoping (Phase 1.1)
# ---------------------------------------------------------------------------


async def test_different_threads_get_different_sessions(channel, captured) -> None:
    """Regression: two threads in the same channel must NOT share a session."""
    await channel._handle_message(
        _event(channel="C1", text="<@UBOT> hi", thread_ts="100.1", ts="101.1")
    )
    await channel._handle_message(
        _event(channel="C1", text="<@UBOT> hi", thread_ts="200.1", ts="201.1")
    )
    assert len(captured) == 2
    assert captured[0].session_id == "slack:C1:100.1"
    assert captured[1].session_id == "slack:C1:200.1"


async def test_same_thread_gets_same_session(channel, captured) -> None:
    """Two replies inside the same thread share one session."""
    await channel._handle_message(
        _event(channel="C1", text="<@UBOT> a", thread_ts="100.1", ts="101.1")
    )
    await channel._handle_message(
        _event(channel="C1", text="<@UBOT> b", thread_ts="100.1", ts="102.1")
    )
    assert len(captured) == 2
    assert captured[0].session_id == captured[1].session_id == "slack:C1:100.1"


async def test_top_level_mentions_isolated_by_ts(channel, captured) -> None:
    """Distinct top-level mentions are isolated from each other (no shared state)."""
    await channel._handle_message(_event(channel="C1", text="<@UBOT> one", ts="300.1"))
    await channel._handle_message(_event(channel="C1", text="<@UBOT> two", ts="301.1"))
    assert len(captured) == 2
    assert captured[0].session_id == "slack:C1:300.1"
    assert captured[1].session_id == "slack:C1:301.1"


async def test_dm_session_stays_channel_scoped_even_in_thread(
    channel, captured
) -> None:
    """DMs keep continuous channel-scoped memory regardless of threading."""
    await channel._handle_message(
        _event(
            channel_type="im", channel="CDM", text="hi", thread_ts="100.1", ts="101.1"
        )
    )
    assert captured[0].session_id == "slack:CDM"


# ---------------------------------------------------------------------------
# Thread-aware addressing (Phase 2.1) — reply-in-active-thread without mention
# ---------------------------------------------------------------------------


async def test_active_thread_addressed_without_mention(channel, captured) -> None:
    """Once the bot has posted into a thread, a plain reply there is addressed."""
    channel._active_threads.add("100.1")
    await channel._handle_message(
        _event(channel="C1", text="follow up", thread_ts="100.1", ts="101.1")
    )
    assert len(captured) == 1
    assert captured[0].content == "follow up"


async def test_inactive_thread_not_addressed_without_mention(channel, captured) -> None:
    """A reply in a thread the bot has not joined still needs a mention."""
    await channel._handle_message(
        _event(channel="C1", text="follow up", thread_ts="100.1", ts="101.1")
    )
    assert captured == []


# ---------------------------------------------------------------------------
# Enriched inbound context (Phase 1.3 / 2.2)
# ---------------------------------------------------------------------------


async def test_context_carries_ts_and_links(channel, captured) -> None:
    await channel._handle_message(
        _event(
            channel_type="im",
            channel="CDM",
            text="see https://example.com/a and <https://example.com/b|b>",
            ts="123.45",
        )
    )
    ctx = captured[0].context
    assert ctx["ts"] == "123.45"
    assert ctx["root_ts"] == ""
    assert "https://example.com/a" in ctx["links"]
    assert "https://example.com/b" in ctx["links"]


async def test_empty_after_mention_strip_ignored(channel, captured) -> None:
    await channel._handle_message(_event(text="<@UBOT>"))  # only the mention
    assert captured == []


async def test_allow_users_allows_listed() -> None:
    # positive path: an allowed user in a DM is captured
    received: list[ChannelMessage] = []

    async def _recv(m: ChannelMessage) -> None:
        received.append(m)

    ch = SlackChannel(
        _recv,
        settings=SlackSettings.model_construct(
            bot_token="x", app_token="y", allow_channels=None, allow_users="UA"
        ),
    )
    ch._bot_user_id = "UBOT"
    await ch._handle_message(
        _event(channel_type="im", channel="CDM", user="UA", text="hi")
    )
    assert len(received) == 1
    assert received[0].content == "hi"
