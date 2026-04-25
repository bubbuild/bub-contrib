"""Bub plugin entry for DingTalk channel."""

from bub import hookimpl
from bub.channels import Channel
from bub.types import MessageHandler

from .channel import DingTalkChannel


@hookimpl
def provide_channels(message_handler: MessageHandler) -> list[Channel]:
    return [DingTalkChannel(message_handler)]
