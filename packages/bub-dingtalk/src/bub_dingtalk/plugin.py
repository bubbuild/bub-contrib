"""Bub plugin entry for DingTalk channel."""

from typing import Any

from bub import hookimpl
from bub import inquirer as bub_inquirer
from bub.channels import Channel
from bub.types import MessageHandler

from .channel import DingTalkChannel

CHANNEL_NAME = "dingtalk"


def _channel_enabled(current_config: dict[str, Any]) -> bool:
    enabled_channels = current_config.get("enabled_channels")
    if not isinstance(enabled_channels, str):
        return True
    value = enabled_channels.strip()
    if not value or value.lower() == "all":
        return True
    return CHANNEL_NAME in {item.strip() for item in value.split(",") if item.strip()}


@hookimpl
def provide_channels(message_handler: MessageHandler) -> list[Channel]:
    return [DingTalkChannel(message_handler)]


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    if not _channel_enabled(current_config):
        return None

    client_id = bub_inquirer.ask_text("DingTalk client ID")
    client_secret = bub_inquirer.ask_secret("DingTalk client secret")
    allow_users = bub_inquirer.ask_text(
        "DingTalk allowed users (comma-separated staff IDs, * for all)",
        default="*",
    )

    return {
        CHANNEL_NAME: {
            "client_id": client_id,
            "client_secret": client_secret,
            "allow_users": allow_users,
        }
    }
