"""Bub plugin entry for the WeCom channel."""

from typing import Any

from bub import hookimpl
from bub import inquirer as bub_inquirer
from bub.channels import Channel
from bub.types import MessageHandler

CHANNEL_NAME = "wecom"
POLICIES = ["open", "disabled", "allowlist"]


def _channel_enabled(current_config: dict[str, Any]) -> bool:
    enabled_channels = current_config.get("enabled_channels")
    if not isinstance(enabled_channels, str):
        return True
    value = enabled_channels.strip()
    if not value or value.lower() == "all":
        return True
    return CHANNEL_NAME in {item.strip() for item in value.split(",") if item.strip()}


def _policy_default(value: object) -> str:
    policy = str(value or "open")
    if policy in POLICIES:
        return policy
    return "open"


@hookimpl
def provide_channels(message_handler: MessageHandler) -> list[Channel]:
    from .channel import WeComChannel

    return [WeComChannel(message_handler)]


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    if not _channel_enabled(current_config):
        return None

    current = current_config.get(CHANNEL_NAME)
    config = current if isinstance(current, dict) else {}
    dm_policy = bub_inquirer.ask_select(
        "WeCom DM policy",
        choices=POLICIES,
        default=_policy_default(config.get("dm_policy")),
    )
    group_policy = bub_inquirer.ask_select(
        "WeCom group policy",
        choices=POLICIES,
        default=_policy_default(config.get("group_policy")),
    )

    return {
        CHANNEL_NAME: {
            "bot_id": bub_inquirer.ask_text(
                "WeCom bot ID",
                default=str(config.get("bot_id") or ""),
            ),
            "secret": bub_inquirer.ask_secret("WeCom secret"),
            "websocket_url": bub_inquirer.ask_text(
                "WeCom websocket URL",
                default=str(
                    config.get("websocket_url") or "wss://openws.work.weixin.qq.com"
                ),
            ),
            "dm_policy": dm_policy,
            "allow_from": bub_inquirer.ask_text(
                "WeCom DM allowlist (optional)",
                default=str(config.get("allow_from") or ""),
            ),
            "group_policy": group_policy,
            "group_allow_from": bub_inquirer.ask_text(
                "WeCom group allowlist (optional)",
                default=str(config.get("group_allow_from") or ""),
            ),
        }
    }
