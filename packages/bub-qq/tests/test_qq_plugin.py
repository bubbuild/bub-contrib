from __future__ import annotations

from bub_qq import plugin


def test_onboard_config_collects_qq_settings(monkeypatch) -> None:
    text_answers = iter(["app-id"])
    secret_answers = iter(["secret"])
    select_answers = iter(["websocket"])
    monkeypatch.setattr(
        plugin.bub_inquirer,
        "ask_text",
        lambda *args, **kwargs: next(text_answers),
    )
    monkeypatch.setattr(
        plugin.bub_inquirer,
        "ask_secret",
        lambda *args, **kwargs: next(secret_answers),
    )
    monkeypatch.setattr(
        plugin.bub_inquirer,
        "ask_select",
        lambda *args, **kwargs: next(select_answers),
    )

    assert plugin.onboard_config({"enabled_channels": "qq"}) == {
        "qq": {
            "appid": "app-id",
            "secret": "secret",
            "receive_mode": "websocket",
        }
    }


def test_onboard_config_skips_when_qq_is_disabled() -> None:
    assert plugin.onboard_config({"enabled_channels": "wecom"}) is None
