"""Inbound event adaptation into Bub ChannelMessage."""

from .c2c import QQC2CInboundService
from .c2c import build_c2c_channel_message
from .group import QQGroupInboundService
from .group import build_group_channel_message

__all__ = [
    "QQC2CInboundService",
    "QQGroupInboundService",
    "build_c2c_channel_message",
    "build_group_channel_message",
]
