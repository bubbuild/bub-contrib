from __future__ import annotations

from pydantic import ValidationError

from bub_qq.config import QQConfig


def test_inbound_dedupe_size_must_be_positive() -> None:
    try:
        QQConfig(receive_mode="webhook", inbound_dedupe_size=0)
    except ValidationError as exc:
        assert "inbound_dedupe_size" in str(exc)
    else:
        raise AssertionError("expected inbound_dedupe_size=0 to be rejected")


def test_unconfigured_qq_section_is_valid() -> None:
    config = QQConfig()

    assert config.appid == ""
    assert config.secret == ""
    assert config.receive_mode == ""


def test_webhook_port_defaults_to_official_allowed_port() -> None:
    config = QQConfig(receive_mode="webhook")

    assert config.webhook_port == 8080


def test_openapi_base_url_defaults_to_unified_endpoint() -> None:
    config = QQConfig()

    assert config.openapi_base_url == "https://api.bot.qq.com"


def test_reply_mode_defaults_to_direct() -> None:
    config = QQConfig()

    assert config.reply_mode == "direct"


def test_reply_mode_rejects_unknown_values() -> None:
    try:
        QQConfig(reply_mode="skill")
    except ValidationError as exc:
        assert "reply_mode" in str(exc)
    else:
        raise AssertionError("expected reply_mode='skill' to be rejected")
