from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Protocol
from typing import Any

from bub.channels.message import ChannelMessage
from loguru import logger

from .models import QQC2CMessage
from .openapi_errors import QQOpenAPIError
from .send_errors import is_duplicate_send_error
from .send_errors import log_send_duplicate_error
from .send_errors import log_send_error


@dataclass
class QQC2CSessionState:
    latest_message_id_by_session: dict[str, str]
    latest_sequence_by_session: dict[str, int]
    latest_timestamp_by_session: dict[str, str]
    send_record_by_session_and_msg_id: dict[tuple[str, str], "QQC2CSendRecord"]


@dataclass
class QQC2CSendRecord:
    content: str
    content_hash: str
    msg_seq: int
    result: dict[str, object]


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


class QQC2COpenAPI(Protocol):
    async def post_c2c_text_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...


class QQC2CInboundService:
    def __init__(self, *, channel_name: str, deduper: QQC2CDeduper, state: QQC2CSessionState) -> None:
        self._channel_name = channel_name
        self._deduper = deduper
        self._state = state

    def parse_inbound(self, payload: dict[str, Any]) -> tuple[QQC2CMessage, ChannelMessage] | None:
        try:
            message = QQC2CMessage.from_event(payload)
        except ValueError as exc:
            logger.warning("qq.c2c.invalid_payload error={}", exc)
            return None

        if self._deduper.seen(message.message_id):
            logger.info("qq.c2c.duplicate message_id={}", message.message_id)
            return None

        channel_message = build_c2c_channel_message(self._channel_name, message)
        remember_c2c_session(
            self._state,
            session_id=channel_message.session_id,
            message_id=message.message_id,
            timestamp=message.timestamp,
            sequence=message.sequence,
        )
        return message, channel_message


class QQC2CSendService:
    def __init__(
        self,
        *,
        channel_name: str,
        receive_mode: str,
        state: QQC2CSessionState,
        openapi: QQC2COpenAPI,
    ) -> None:
        self._channel_name = channel_name
        self._receive_mode = receive_mode
        self._state = state
        self._openapi = openapi

    async def send(self, message: ChannelMessage) -> dict[str, object] | None:
        content = normalize_c2c_outbound_content(message.content or "")
        if not content:
            logger.warning("qq.send skip_empty session_id={}", message.session_id)
            return None

        session_id = message.session_id or ""
        chat_id = message.chat_id or ""
        openid = resolve_c2c_openid(
            channel_name=self._channel_name,
            session_id=session_id,
            chat_id=chat_id,
        )
        if not openid:
            logger.warning(
                "qq.send unresolved_openid session_id={} chat_id={}",
                message.session_id,
                message.chat_id,
            )
            return None

        msg_id = self._state.latest_message_id_by_session.get(session_id)
        if not msg_id:
            logger.warning(
                "qq.send missing_msg_id session_id={} reason=active_push_not_supported",
                session_id,
            )
            return None

        if not is_passive_reply_window_open(self._state, session_id):
            logger.warning(
                "qq.send passive_reply_window_expired session_id={} msg_id={}",
                session_id,
                msg_id,
            )
            return None

        content_hash = hash_c2c_content(content)
        send_record = self._state.send_record_by_session_and_msg_id.get((session_id, msg_id))
        if send_record is not None:
            if send_record.content_hash == content_hash:
                logger.info(
                    "qq.send duplicate session_id={} openid={} msg_id={} reason=already_sent source=local_dedup_hit msg_seq={} content_hash={}",
                    session_id,
                    openid,
                    msg_id,
                    send_record.msg_seq,
                    content_hash,
                )
                return build_already_sent_result(send_record)
            logger.warning(
                "qq.send duplicate session_id={} openid={} msg_id={} reason=duplicate_reply_blocked source=local_dedup_hit previous_msg_seq={} previous_content_hash={} content_hash={}",
                session_id,
                openid,
                msg_id,
                send_record.msg_seq,
                send_record.content_hash,
                content_hash,
            )
            return {"status": "duplicate_reply_blocked"}

        msg_seq = next_c2c_msg_seq(self._state, session_id)
        try:
            result = await self._openapi.post_c2c_text_message(
                openid=openid,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
        except QQOpenAPIError as exc:
            if is_duplicate_send_error(exc):
                log_send_duplicate_error(
                    exc,
                    session_id=session_id,
                    openid=openid,
                    msg_id=msg_id,
                    msg_seq=msg_seq,
                    content_hash=content_hash,
                )
                duplicate_record = QQC2CSendRecord(
                    content=content,
                    content_hash=content_hash,
                    msg_seq=msg_seq,
                    result={},
                )
                self._state.send_record_by_session_and_msg_id[(session_id, msg_id)] = duplicate_record
                return build_already_sent_result(duplicate_record)
            log_send_error(
                exc,
                session_id=session_id,
                openid=openid,
                msg_id=msg_id,
                msg_seq=msg_seq,
                receive_mode=self._receive_mode,
            )
            return None

        send_record = QQC2CSendRecord(
            content=content,
            content_hash=content_hash,
            msg_seq=msg_seq,
            result=dict(result),
        )
        self._state.send_record_by_session_and_msg_id[(session_id, msg_id)] = send_record
        logger.info(
            "qq.send success session_id={} openid={} msg_id={} msg_seq={} response_id={}",
            session_id,
            openid,
            msg_id,
            msg_seq,
            result.get("id"),
        )
        return result


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


def build_already_sent_result(send_record: QQC2CSendRecord) -> dict[str, object]:
    result = dict(send_record.result)
    result["status"] = "already_sent"
    return result


def hash_c2c_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def normalize_c2c_outbound_content(content: str) -> str:
    normalized = content.strip()
    normalized = re.sub(r"^\$qq\s*→\s*", "", normalized, count=1, flags=re.IGNORECASE)
    return normalized.strip()


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
