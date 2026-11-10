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


def _accept_defaults(monkeypatch) -> None:
    monkeypatch.setattr(
        plugin.bub_inquirer, "ask_text", lambda message, default="": default
    )
    monkeypatch.setattr(plugin.bub_inquirer, "ask_secret", lambda message: "")
    monkeypatch.setattr(
        plugin.bub_inquirer,
        "ask_select",
        lambda message, choices, default="": default,
    )


def test_onboard_config_keeps_existing_qq_settings(monkeypatch) -> None:
    _accept_defaults(monkeypatch)
    existing = {
        "appid": "app-id",
        "secret": "secret",
        "receive_mode": "websocket",
    }

    assert plugin.onboard_config({"enabled_channels": "qq", "qq": existing}) == {
        "qq": existing
    }


def test_onboard_config_skips_when_qq_is_disabled() -> None:
    assert plugin.onboard_config({"enabled_channels": "wecom"}) is None
