from __future__ import annotations

from bub_feishu import plugin


def test_onboard_config_collects_feishu_settings(monkeypatch) -> None:
    text_answers = iter(["app-id", "user-1", "chat-1", "bot-open-id"])
    secret_answers = iter(["app-secret", "verification-token", "encrypt-key"])
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
        lambda *args, **kwargs: "INFO",
    )

    assert plugin.onboard_config({"enabled_channels": "feishu"}) == {
        "feishu": {
            "app_id": "app-id",
            "app_secret": "app-secret",
            "verification_token": "verification-token",
            "encrypt_key": "encrypt-key",
            "allow_users": "user-1",
            "allow_chats": "chat-1",
            "bot_open_id": "bot-open-id",
            "log_level": "INFO",
        }
    }


def test_onboard_config_skips_when_feishu_is_disabled() -> None:
    assert plugin.onboard_config({"enabled_channels": "discord"}) is None
