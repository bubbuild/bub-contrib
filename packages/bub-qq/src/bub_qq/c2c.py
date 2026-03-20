from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from bub.channels.message import ChannelMessage

from .models import QQC2CMessage


@dataclass
class QQC2CSessionState:
    latest_message_id_by_session: dict[str, str]
    latest_sequence_by_session: dict[str, int]
    latest_timestamp_by_session: dict[str, str]


class QQC2CDeduper:
    """Bounded recent-message cache for duplicate QQ deliveries."""

    def __init__(self, size: int) -> None:
        self._ids: deque[str] = deque(maxlen=size)
        self._id_set: set[str] = set()

    def seen(self, message_id: str) -> bool:
        if message_id in self._id_set:
            return True
        evicted: str | None = None
        if len(self._ids) == self._ids.maxlen:
            evicted = self._ids[0]
        self._ids.append(message_id)
        self._id_set.add(message_id)
        if evicted is not None and evicted not in self._ids:
            self._id_set.discard(evicted)
        return False


def build_c2c_channel_message(channel_name: str, message: QQC2CMessage) -> ChannelMessage:
    session_id = f"{channel_name}:c2c:{message.user_openid}"
    chat_id = f"c2c:{message.user_openid}"
    text = message.content.strip()

    if text.startswith(","):
        return ChannelMessage(
            session_id=session_id,
            content=text,
            channel=channel_name,
            chat_id=chat_id,
            kind="command",
            is_active=True,
        )

    payload = {
        "message": message.content,
        "message_id": message.message_id,
        "type": "text" if not message.attachments else "attachment",
        "sender_id": message.user_openid,
        "date": message.timestamp,
        "attachments": [
            {
                "content_type": attachment.content_type,
                "filename": attachment.filename,
                "height": attachment.height,
                "width": attachment.width,
                "size": attachment.size,
                "url": attachment.url,
                "voice_wav_url": attachment.voice_wav_url,
                "asr_refer_text": attachment.asr_refer_text,
            }
            for attachment in message.attachments
        ]
        or None,
    }
    return ChannelMessage(
        session_id=session_id,
        content=json.dumps(exclude_none(payload), ensure_ascii=False),
        channel=channel_name,
        chat_id=chat_id,
        is_active=True,
        output_channel="null",
    )


def remember_c2c_session(
    state: QQC2CSessionState,
    *,
    session_id: str,
    message_id: str,
    timestamp: str | None,
    sequence: int | None,
) -> None:
    state.latest_message_id_by_session[session_id] = message_id
    if timestamp is not None:
        state.latest_timestamp_by_session[session_id] = timestamp
    if sequence is not None:
        state.latest_sequence_by_session[session_id] = sequence


def resolve_c2c_openid(*, channel_name: str, session_id: str, chat_id: str) -> str | None:
    if chat_id.startswith("c2c:"):
        openid = chat_id.removeprefix("c2c:").strip()
        return openid or None
    prefix = f"{channel_name}:c2c:"
    if session_id.startswith(prefix):
        openid = session_id.removeprefix(prefix).strip()
        return openid or None
    return None


def next_c2c_msg_seq(state: QQC2CSessionState, session_id: str) -> int:
    current = state.latest_sequence_by_session.get(session_id, 0) + 1
    state.latest_sequence_by_session[session_id] = current
    return current


def is_passive_reply_window_open(state: QQC2CSessionState, session_id: str) -> bool:
    timestamp = state.latest_timestamp_by_session.get(session_id)
    if not timestamp:
        return True
    try:
        sent_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return datetime.now(sent_at.tzinfo) - sent_at <= timedelta(minutes=60)


def exclude_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
