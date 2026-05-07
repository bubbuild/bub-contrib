from typing import Any

from bub import hookimpl
from bub import inquirer as bub_inquirer
from bub.channels import Channel
from bub.types import MessageHandler

CHANNEL_NAME = "feishu"


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
    from .channel import FeishuChannel

    return [FeishuChannel(message_handler)]


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    if not _channel_enabled(current_config):
        return None

    app_id = bub_inquirer.ask_text("Feishu app ID")
    app_secret = bub_inquirer.ask_secret("Feishu app secret")
    verification_token = bub_inquirer.ask_secret("Feishu verification token")
    encrypt_key = bub_inquirer.ask_secret("Feishu encrypt key")
    allow_users = bub_inquirer.ask_text("Feishu allowed users (optional)")
    allow_chats = bub_inquirer.ask_text("Feishu allowed chats (optional)")
    bot_open_id = bub_inquirer.ask_text("Feishu bot open ID (optional)")
    log_level = bub_inquirer.ask_text("Feishu log level", default="INFO")

    return {
        CHANNEL_NAME: {
            "app_id": app_id,
            "app_secret": app_secret,
            "verification_token": verification_token,
            "encrypt_key": encrypt_key,
            "allow_users": allow_users,
            "allow_chats": allow_chats,
            "bot_open_id": bot_open_id,
            "log_level": log_level or "INFO",
        }
    }
