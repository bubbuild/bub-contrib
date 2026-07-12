from __future__ import annotations

import os

import pytest
from bub.configure import _global_config, ensure_config
from bub_slack.config import SlackSettings


def _clear() -> None:
    # ``ensure_config`` caches resolved settings; clear it between cases so env
    # changes are picked up freshly.
    _global_config.clear()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Strip every slack-related var so each test starts from a known baseline.
    for key in list(os.environ):
        if "SLACK" in key.upper():
            monkeypatch.delenv(key, raising=False)
    _clear()


def test_env_prefix_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_SLACK_BOT_TOKEN", "xoxb-real")
    monkeypatch.setenv("BUB_SLACK_APP_TOKEN", "xapp-real")
    _clear()
    s = ensure_config(SlackSettings)
    assert s.bot_token == "xoxb-real"
    assert s.app_token == "xapp-real"


def test_allow_lists_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_SLACK_ALLOW_CHANNELS", "C1,C2")
    monkeypatch.setenv("BUB_SLACK_ALLOW_USERS", "U1")
    _clear()
    s = ensure_config(SlackSettings)
    assert s.allow_channels == "C1,C2"
    assert s.allow_users == "U1"


def test_env_overrides_yaml_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_SLACK_BOT_TOKEN", "xoxb-env")
    monkeypatch.setenv("BUB_SLACK_APP_TOKEN", "xapp-env")
    _clear()
    s = SlackSettings.model_validate(
        {
            "bot_token": "xoxb-yaml",
            "app_token": "xapp-yaml",
            "allow_channels": "C1,C2",
        }
    )
    assert s.bot_token == "xoxb-env"
    assert s.app_token == "xapp-env"
    assert s.allow_channels == "C1,C2"


def test_empty_tokens_default_disabled() -> None:
    _clear()
    s = ensure_config(SlackSettings)
    assert s.bot_token == ""
    assert s.app_token == ""
    assert not (bool(s.bot_token) and bool(s.app_token))
