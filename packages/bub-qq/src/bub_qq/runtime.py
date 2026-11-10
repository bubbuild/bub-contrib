"""Process-wide handle to the running QQ channel.

The ``qq.send`` tool executes inside the agent loop but must reach the
channel's send services (which own the passive-reply session state). The
channel registers itself here on construction; the tool looks it up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .channel import QQChannel

_active_channel: QQChannel | None = None


def set_active_channel(channel: QQChannel | None) -> None:
    global _active_channel
    _active_channel = channel


def get_active_channel() -> QQChannel | None:
    return _active_channel
