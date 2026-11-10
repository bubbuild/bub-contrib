from __future__ import annotations

from bub_dingtalk import plugin


def test_onboard_config_collects_dingtalk_settings(monkeypatch) -> None:
    text_answers = iter(["client-id", "*"])
    secret_answers = iter(["client-secret"])
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

    assert plugin.onboard_config({"enabled_channels": "dingtalk"}) == {
        "dingtalk": {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "allow_users": "*",
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


def test_onboard_config_keeps_existing_dingtalk_settings(monkeypatch) -> None:
    _accept_defaults(monkeypatch)
    existing = {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "allow_users": "staff-1",
    }

    assert plugin.onboard_config(
        {"enabled_channels": "dingtalk", "dingtalk": existing}
    ) == {"dingtalk": existing}


def test_onboard_config_skips_when_dingtalk_is_disabled() -> None:
    assert plugin.onboard_config({"enabled_channels": "discord"}) is None
