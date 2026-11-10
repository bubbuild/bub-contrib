"""Shared per-session state for QQ inbound tracking and passive replies.

All state containers here are bounded so a long-running process cannot
grow without limit: once ``max_entries`` is exceeded, the least recently
written entries are evicted. Losing an old entry only means an old
session can no longer receive a passive reply, which QQ would reject
anyway once its reply window expires.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from .protocol.models import QQAttachment

K = TypeVar("K")
V = TypeVar("V")


class BoundedDict(OrderedDict[K, V]):
    """Dict that evicts the least recently written entries beyond a limit."""

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        super().__init__()
        self._max_entries = max_entries

    def __setitem__(self, key: K, value: V) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self._max_entries:
            self.popitem(last=False)


@dataclass
class QQSendRecord:
    """Result of a completed outbound send, kept for local deduplication."""

    content: str
    content_hash: str
    msg_seq: int
    result: dict[str, object]


class QQSessionState:
    """Per-session reply context shared by C2C and group send paths.

    ``send_records`` is keyed by ``(session_id, msg_id, content_hash)`` so a
    retried delivery of identical content for the same inbound message is
    recognized locally instead of hitting QQ's remote deduplication error.
    """

    def __init__(self, *, max_entries: int = 1024) -> None:
        self.latest_message_id_by_session: BoundedDict[str, str] = BoundedDict(
            max_entries
        )
        self.latest_timestamp_by_session: BoundedDict[str, str] = BoundedDict(
            max_entries
        )
        self.latest_sequence_by_session_and_msg_id: BoundedDict[
            tuple[str, str], int
        ] = BoundedDict(max_entries)
        self.send_records: BoundedDict[tuple[str, str, str], QQSendRecord] = (
            BoundedDict(max_entries)
        )
        # message_id -> (session_id, timestamp): lets a reply target the
        # message that triggered it rather than the latest one in the chat.
        self.message_by_id: BoundedDict[str, tuple[str, str]] = BoundedDict(
            max_entries
        )
        # message_id -> (session_id, attachments), for on-demand download.
        self.attachments_by_message_id: BoundedDict[
            str, tuple[str, tuple[QQAttachment, ...]]
        ] = BoundedDict(max_entries)


class QQInboundDeduper:
    """Bounded recent-message cache for duplicate QQ deliveries."""

    def __init__(self, size: int) -> None:
        self._seen: BoundedDict[str, None] = BoundedDict(size)

    def seen(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        return False


def remember_session(
    state: QQSessionState,
    *,
    session_id: str,
    message_id: str,
    timestamp: str | None,
    attachments: tuple[QQAttachment, ...] = (),
) -> None:
    # Always write both "latest" maps together so they evict in lockstep;
    # an empty timestamp means unknown (the window check then fails open).
    state.latest_message_id_by_session[session_id] = message_id
    state.latest_timestamp_by_session[session_id] = timestamp or ""
    state.message_by_id[message_id] = (session_id, timestamp or "")
    if attachments:
        state.attachments_by_message_id[message_id] = (session_id, attachments)
