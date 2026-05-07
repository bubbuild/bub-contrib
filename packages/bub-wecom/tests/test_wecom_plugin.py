from __future__ import annotations

from bub_wecom import plugin


def test_onboard_config_collects_wecom_settings(monkeypatch) -> None:
    text_answers = iter(["bot-id", "wss://openws.work.weixin.qq.com", "alice", "room-1"])
    secret_answers = iter(["secret"])
    select_answers = iter(["allowlist", "allowlist"])
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

    assert plugin.onboard_config({"enabled_channels": "wecom"}) == {
        "wecom": {
            "bot_id": "bot-id",
            "secret": "secret",
            "websocket_url": "wss://openws.work.weixin.qq.com",
            "dm_policy": "allowlist",
            "allow_from": "alice",
            "group_policy": "allowlist",
            "group_allow_from": "room-1",
        }
    }


def test_onboard_config_skips_when_wecom_is_disabled() -> None:
    assert plugin.onboard_config({"enabled_channels": "qq"}) is None
