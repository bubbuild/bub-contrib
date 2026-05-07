from __future__ import annotations

from bub_discord import plugin


def test_onboard_config_collects_discord_settings(monkeypatch) -> None:
    text_answers = iter(["user-1,user-2", "channel-1", "!", ""])
    secret_answers = iter(["bot-token"])
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

    assert plugin.onboard_config({"enabled_channels": "discord"}) == {
        "discord": {
            "token": "bot-token",
            "allow_users": "user-1,user-2",
            "allow_channels": "channel-1",
            "command_prefix": "!",
            "proxy": "",
        }
    }


def test_onboard_config_skips_when_discord_is_disabled() -> None:
    assert plugin.onboard_config({"enabled_channels": "feishu"}) is None
