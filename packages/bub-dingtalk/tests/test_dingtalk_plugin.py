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


def test_onboard_config_skips_when_dingtalk_is_disabled() -> None:
    assert plugin.onboard_config({"enabled_channels": "discord"}) is None
