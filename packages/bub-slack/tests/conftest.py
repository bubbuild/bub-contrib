from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from bub.channels.message import ChannelMessage
from bub_slack.channel import _ACK_PENDING, SlackChannel
from bub_slack.config import SlackSettings


@pytest.fixture(autouse=True)
def _isolate_ack_pending() -> None:
    # The inbound-ack map is module-level shared state; keep tests independent.
    _ACK_PENDING.clear()


@pytest.fixture
def settings() -> SlackSettings:
    # ``model_construct`` bypasses ALL settings sources (env vars + .env file)
    # so the fixture is deterministic regardless of a real project .env.
    return SlackSettings.model_construct(
        bot_token="xoxb-test",
        app_token="xapp-test",
        allow_channels=None,
        allow_users=None,
    )


@pytest.fixture
def captured() -> list[ChannelMessage]:
    return []


@pytest.fixture
def on_receive(
    captured: list[ChannelMessage],
) -> Callable[[ChannelMessage], Awaitable[None]]:
    async def _recv(message: ChannelMessage) -> None:
        captured.append(message)

    return _recv


@pytest.fixture
def channel(on_receive, settings) -> SlackChannel:
    ch = SlackChannel(on_receive, settings=settings)
    # Pretend auth_test already resolved the bot id (used by the echo guard + mention strip).
    ch._bot_user_id = "UBOT"
    return ch
