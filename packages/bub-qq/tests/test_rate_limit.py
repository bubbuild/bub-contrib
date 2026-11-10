from __future__ import annotations

import asyncio

import bub
import pytest
from bub.channels.message import ChannelMessage

from bub_qq import plugin
from bub_qq.config import QQConfig
from bub_qq.runtime import set_active_channel


class FakeChannel:
    name = "qq"

    def __init__(self) -> None:
        self.messages: list[ChannelMessage] = []

    async def send_for_result(self, message: ChannelMessage) -> dict[str, object]:
        self.messages.append(message)
        return {"id": "sent"}


@pytest.fixture
def setup(monkeypatch):
    plugin._rate_limiter = None
    plugin._rate_notified.clear()
    current = {
        "config": QQConfig.model_construct(
            llm_rate_limit_per_minute=1,
            llm_rate_limit_notice="慢一点",
            reply_mode="tool",
        )
    }
    monkeypatch.setattr(bub, "ensure_config", lambda cls: current["config"])
    channel = FakeChannel()
    set_active_channel(channel)
    yield current, channel
    set_active_channel(None)
    plugin._rate_limiter = None
    plugin._rate_notified.clear()


def _turn() -> dict:
    return {
        "qq": {
            "scope": "group",
            "sender_id": "m1",
            "group_openid": "g1",
            "session_id": "qq:group:g1",
        }
    }


def _call(state: dict):
    return asyncio.run(plugin.before_llm_call(None, state))


def test_limit_counts_turns_not_llm_steps(setup) -> None:
    turn = _turn()
    # One turn with three tool steps is one unit of the budget.
    assert [_call(turn) for _ in range(3)] == [None, None, None]
    blocked = _call(_turn())
    assert blocked is not None and blocked.action == "finish"


def test_tool_mode_sends_the_notice_once_per_window(setup) -> None:
    _, channel = setup
    _call(_turn())
    _call(_turn())
    _call(_turn())
    assert [m.content for m in channel.messages] == ["慢一点"]
    assert channel.messages[0].chat_id == "group:g1"


def test_direct_mode_relies_on_the_finish_text(setup) -> None:
    current, channel = setup
    current["config"] = QQConfig.model_construct(
        llm_rate_limit_per_minute=1, llm_rate_limit_notice="慢一点", reply_mode="direct"
    )
    _call(_turn())
    blocked = _call(_turn())
    assert blocked is not None and blocked.text == "慢一点"
    assert channel.messages == []


def test_changed_limit_takes_effect_without_restart(setup) -> None:
    current, _ = setup
    _call(_turn())
    assert _call(_turn()) is not None
    current["config"] = QQConfig.model_construct(
        llm_rate_limit_per_minute=5, llm_rate_limit_notice="慢一点", reply_mode="tool"
    )
    assert _call(_turn()) is None
