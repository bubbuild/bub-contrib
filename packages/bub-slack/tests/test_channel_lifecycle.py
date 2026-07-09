from __future__ import annotations

import asyncio
from typing import Any

import pytest
from bub_slack.channel import SlackChannel
from bub_slack.config import SlackSettings


def _settings(bot: str = "xoxb", app: str = "xapp") -> SlackSettings:
    # ``model_construct`` bypasses settings sources so tests are deterministic.
    return SlackSettings.model_construct(bot_token=bot, app_token=app)


def test_enabled_true_with_both_tokens() -> None:
    ch = SlackChannel(on_receive=None, settings=_settings())  # type: ignore[arg-type]
    assert ch.enabled is True


def test_enabled_false_without_tokens() -> None:
    ch = SlackChannel(on_receive=None, settings=_settings(bot="", app="xapp"))  # type: ignore[arg-type]
    assert ch.enabled is False
    ch2 = SlackChannel(on_receive=None, settings=_settings(bot="xoxb", app=""))  # type: ignore[arg-type]
    assert ch2.enabled is False


def test_needs_debounce_true() -> None:
    ch = SlackChannel(on_receive=None, settings=_settings())  # type: ignore[arg-type]
    assert ch.needs_debounce is True


@pytest.mark.asyncio
async def test_stop_with_no_client_is_safe() -> None:
    ch = SlackChannel(on_receive=None, settings=_settings())  # type: ignore[arg-type]
    # _client is None — stop() must not raise
    await ch.stop()


@pytest.mark.asyncio
async def test_stop_closes_client() -> None:
    ch = SlackChannel(on_receive=None, settings=_settings())  # type: ignore[arg-type]

    class _FakeClient:
        closed = 0

        async def close(self) -> None:
            self.closed += 1

    fake = _FakeClient()
    ch._client = fake  # type: ignore[assignment]
    await ch.stop()
    assert fake.closed == 1


@pytest.mark.asyncio
async def test_start_returns_fast_and_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for the critical blocking bug: ``start()`` must NOT await
    ``stop_event.wait()`` — the ChannelManager runs ``start()`` sequentially
    before the consumer loop, so blocking here deadlocks message processing.
    """

    ch = SlackChannel(on_receive=None, settings=_settings())  # type: ignore[arg-type]

    class _FakeAuth:
        async def auth_test(self) -> dict[str, Any]:
            return {"user_id": "UBOT", "user": "test-bot", "team": "T", "url": "u"}

    class _FakeSM:
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.connected = 0
            self.socket_mode_request_listeners: list[Any] = []

        async def connect(self) -> None:
            self.connected += 1

        async def close(self) -> None:
            pass

    monkeypatch.setattr("bub_slack.channel.AsyncWebClient", lambda **kw: _FakeAuth())
    monkeypatch.setattr("bub_slack.channel.SocketModeClient", _FakeSM)

    stop_event = asyncio.Event()
    # If start() blocks on stop_event, this times out at 1s.
    await asyncio.wait_for(ch.start(stop_event), timeout=1.0)
    assert ch._bot_user_id == "UBOT"
    assert ch._client is not None and ch._client.connected == 1  # type: ignore[union-attr]


def _patch_slack_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeAuth:
        async def auth_test(self) -> dict[str, Any]:
            return {"user_id": "UBOT", "user": "test-bot", "team": "T", "url": "u"}

    class _FakeSM:
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.connected = 0
            self.socket_mode_request_listeners: list[Any] = []

        async def connect(self) -> None:
            self.connected += 1

        async def close(self) -> None:
            pass

    monkeypatch.setattr("bub_slack.channel.AsyncWebClient", lambda **kw: _FakeAuth())
    monkeypatch.setattr("bub_slack.channel.SocketModeClient", _FakeSM)


@pytest.mark.asyncio
async def test_health_marker_touched_on_start_cleared_on_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The k8s startup/readiness probe execs ``test -f $BUB_HEALTH_FILE``; the
    marker must appear once Socket Mode connects and disappear on stop()."""
    marker = tmp_path / "slack.ready"
    monkeypatch.setenv("BUB_HEALTH_FILE", str(marker))

    ch = SlackChannel(on_receive=None, settings=_settings())  # type: ignore[arg-type]
    _patch_slack_clients(monkeypatch)

    assert not marker.exists()
    await asyncio.wait_for(ch.start(asyncio.Event()), timeout=1.0)
    assert marker.exists()
    assert marker.read_text() == "ready\n"

    await ch.stop()
    assert not marker.exists()
