from typing import Any

from bub import hookimpl
from bub import inquirer as bub_inquirer
from bub.channels import Channel
from bub.types import MessageHandler

CHANNEL_NAME = "qq"
RECEIVE_MODES = ["webhook", "websocket"]


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
    from .channel import QQChannel

    return [QQChannel(message_handler)]


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    if not _channel_enabled(current_config):
        return None

    current = current_config.get(CHANNEL_NAME)
    config = current if isinstance(current, dict) else {}
    receive_mode_default = str(config.get("receive_mode") or "webhook")
    if receive_mode_default not in RECEIVE_MODES:
        receive_mode_default = "webhook"

    return {
        CHANNEL_NAME: {
            "appid": bub_inquirer.ask_text(
                "QQ app ID",
                default=str(config.get("appid") or ""),
            ),
            "secret": bub_inquirer.ask_secret("QQ secret"),
            "receive_mode": bub_inquirer.ask_select(
                "QQ receive mode",
                choices=RECEIVE_MODES,
                default=receive_mode_default,
            ),
        }
    }
