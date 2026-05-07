from typing import Any

from bub import hookimpl
from bub import inquirer as bub_inquirer
from bub.channels import Channel
from bub.types import MessageHandler

CHANNEL_NAME = "discord"


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
    from .channel import DiscordChannel

    return [DiscordChannel(message_handler)]


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    if not _channel_enabled(current_config):
        return None

    token = bub_inquirer.ask_secret("Discord bot token")
    allow_users = bub_inquirer.ask_text("Discord allowed users (optional)")
    allow_channels = bub_inquirer.ask_text("Discord allowed channels (optional)")
    command_prefix = bub_inquirer.ask_text("Discord command prefix", default="!")
    proxy = bub_inquirer.ask_text("Discord proxy (optional)")

    return {
        CHANNEL_NAME: {
            "token": token,
            "allow_users": allow_users,
            "allow_channels": allow_channels,
            "command_prefix": command_prefix or "!",
            "proxy": proxy,
        }
    }
