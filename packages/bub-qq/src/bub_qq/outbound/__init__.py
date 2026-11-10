"""Outbound delivery to QQ OpenAPI."""

from .c2c import QQC2CSendService
from .group import QQGroupSendService

__all__ = [
    "QQC2CSendService",
    "QQGroupSendService",
]
