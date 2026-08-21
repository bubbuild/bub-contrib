"""Outbound send flow shared by the C2C and group send services.

Three-tier decision, passive first:

1. **Passive reply** — an inbound ``msg_id`` exists, its reply window is
   open, and the local per-``msg_id`` reply cap is not reached: reply
   with ``msg_id`` + plugin-managed ``msg_seq`` (never consumes active
   quota).
2. **Active fallback** — otherwise, when the service provides an active
   sender (group scope with ``active_messages`` enabled) and the platform
   store does not say the admin rejected active messages, send a
   proactive plain-text message without ``msg_id``/``msg_seq``.
3. **Skip** — no passive context and no active path: log and drop.

Both paths share content-hash deduplication and result recording. Only
target resolution and the OpenAPI calls differ, so the services inject
those as callables.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Protocol

from loguru import logger

from ..protocol.errors import QQOpenAPIError
from ..session import QQSendRecord
from ..session import QQSessionState
from .markdown import MarkdownSender
from .markdown import send_with_markdown_fallback
from .send_errors import is_duplicate_send_error
from .send_errors import is_pending_audit_error
from .send_errors import log_send_duplicate_error
from .send_errors import log_send_error

DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS = 3600.0
DEFAULT_PASSIVE_REPLIES_PER_MSG_ID = 4

# Pseudo msg_id used to key dedupe records for active (proactive) sends.
ACTIVE_MSG_ID = "__active__"

# Sentinel the model outputs (per the injected system prompt) to skip the
# reply for this turn. Matched leniently: <no_reply/>, <no_reply>, <no_reply />.
NO_REPLY_SENTINEL = "<no_reply/>"
_NO_REPLY_RE = re.compile(r"^<no_reply\s*/?>", re.IGNORECASE)

# Model special tokens (<|eos|>, <|im_end|>, <|endoftext|>, ...) sometimes
# leak into text output, typically when the model means "nothing to say".
# They are stripped from the edges of outbound content; a reply that was
# only special tokens becomes empty and is skipped.
_EDGE_SPECIAL_TOKEN_RE = re.compile(r"^\s*<\|[^|<>]{1,64}\|>\s*|\s*<\|[^|<>]{1,64}\|>\s*$")


def is_no_reply(content: str) -> bool:
    """Whether the model chose silence for this turn (sentinel protocol)."""

    return bool(_NO_REPLY_RE.match(content.strip()))


def _strip_edge_special_tokens(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _EDGE_SPECIAL_TOKEN_RE.sub("", text)
    return text.strip()


class ActiveSender(Protocol):
    async def __call__(self, *, content: str) -> dict[str, object]: ...


async def run_send_flow(
    *,
    state: QQSessionState,
    receive_mode: str,
    session_id: str,
    target_openid: str,
    content: str,
    send_text: MarkdownSender,
    send_markdown: MarkdownSender,
    passive_reply_window_seconds: float = DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS,
    passive_replies_per_msg_id: int = DEFAULT_PASSIVE_REPLIES_PER_MSG_ID,
    send_active_text: ActiveSender | None = None,
    active_messages_allowed: bool | None = None,
) -> dict[str, object] | None:
    msg_id = state.latest_message_id_by_session.get(session_id)
    passive_blocked_reason = _passive_blocked_reason(
        state,
        session_id=session_id,
        msg_id=msg_id,
        window_seconds=passive_reply_window_seconds,
        replies_per_msg_id=passive_replies_per_msg_id,
    )
    if passive_blocked_reason is None and msg_id:
        return await _run_passive_send(
            state=state,
            receive_mode=receive_mode,
            session_id=session_id,
            target_openid=target_openid,
            content=content,
            msg_id=msg_id,
            send_text=send_text,
            send_markdown=send_markdown,
        )

    logger.info(
        "qq.send passive_unavailable session_id={} msg_id={} reason={}",
        session_id,
        msg_id or "-",
        passive_blocked_reason,
    )
    if send_active_text is None:
        logger.warning(
            "qq.send skipped session_id={} reason={} active=unavailable",
            session_id,
            passive_blocked_reason,
        )
        return None
    if active_messages_allowed is False:
        logger.warning(
            "qq.send skipped session_id={} reason=active_rejected_by_admin",
            session_id,
        )
        return None
    return await _run_active_send(
        state=state,
        session_id=session_id,
        target_openid=target_openid,
        content=content,
        send_active_text=send_active_text,
    )


async def _run_passive_send(
    *,
    state: QQSessionState,
    receive_mode: str,
    session_id: str,
    target_openid: str,
    content: str,
    msg_id: str,
    send_text: MarkdownSender,
    send_markdown: MarkdownSender,
) -> dict[str, object] | None:
    content_hash = hash_outbound_content(content)
    record_key = (session_id, msg_id, content_hash)
    existing = state.send_records.get(record_key)
    if existing is not None:
        logger.info(
            "qq.send duplicate session_id={} openid={} msg_id={} reason=already_sent source=local_dedup_hit msg_seq={} content_hash={}",
            session_id,
            target_openid,
            msg_id,
            existing.msg_seq,
            content_hash,
        )
        return build_already_sent_result(existing)

    msg_seq = next_msg_seq(state, session_id, msg_id)
    try:
        result = await send_with_markdown_fallback(
            content=content,
            msg_id=msg_id,
            msg_seq=msg_seq,
            send_text=send_text,
            send_markdown=send_markdown,
        )
    except QQOpenAPIError as exc:
        if is_duplicate_send_error(exc):
            log_send_duplicate_error(
                exc,
                session_id=session_id,
                openid=target_openid,
                msg_id=msg_id,
                msg_seq=msg_seq,
                content_hash=content_hash,
            )
            duplicate_record = QQSendRecord(
                content=content,
                content_hash=content_hash,
                msg_seq=msg_seq,
                result={},
            )
            state.send_records[record_key] = duplicate_record
            return build_already_sent_result(duplicate_record)
        if is_pending_audit_error(exc):
            return _record_pending_audit(
                state,
                record_key=record_key,
                content=content,
                content_hash=content_hash,
                msg_seq=msg_seq,
                session_id=session_id,
                target_openid=target_openid,
                error_code=exc.error_code,
            )
        log_send_error(
            exc,
            session_id=session_id,
            openid=target_openid,
            msg_id=msg_id,
            msg_seq=msg_seq,
            receive_mode=receive_mode,
        )
        return None

    state.send_records[record_key] = QQSendRecord(
        content=content,
        content_hash=content_hash,
        msg_seq=msg_seq,
        result=dict(result),
    )
    logger.info(
        "qq.send success session_id={} openid={} msg_id={} msg_seq={} response_id={}",
        session_id,
        target_openid,
        msg_id,
        msg_seq,
        result.get("id"),
    )
    return result


async def _run_active_send(
    *,
    state: QQSessionState,
    session_id: str,
    target_openid: str,
    content: str,
    send_active_text: ActiveSender,
) -> dict[str, object] | None:
    content_hash = hash_outbound_content(content)
    record_key = (session_id, ACTIVE_MSG_ID, content_hash)
    existing = state.send_records.get(record_key)
    if existing is not None:
        logger.info(
            "qq.send duplicate session_id={} openid={} mode=active reason=already_sent source=local_dedup_hit content_hash={}",
            session_id,
            target_openid,
            content_hash,
        )
        return build_already_sent_result(existing)

    try:
        result = await send_active_text(content=content)
    except QQOpenAPIError as exc:
        if is_pending_audit_error(exc):
            return _record_pending_audit(
                state,
                record_key=record_key,
                content=content,
                content_hash=content_hash,
                msg_seq=0,
                session_id=session_id,
                target_openid=target_openid,
                error_code=exc.error_code,
            )
        logger.warning(
            "qq.send failed session_id={} openid={} mode=active code={} trace_id={} error={}",
            session_id,
            target_openid,
            exc.error_code,
            exc.trace_id or "-",
            exc.error_message,
        )
        return None

    state.send_records[record_key] = QQSendRecord(
        content=content,
        content_hash=content_hash,
        msg_seq=0,
        result=dict(result),
    )
    logger.info(
        "qq.send success session_id={} openid={} mode=active response_id={}",
        session_id,
        target_openid,
        result.get("id"),
    )
    return result


def _passive_blocked_reason(
    state: QQSessionState,
    *,
    session_id: str,
    msg_id: str | None,
    window_seconds: float,
    replies_per_msg_id: int,
) -> str | None:
    if not msg_id:
        return "missing_msg_id"
    if not is_passive_reply_window_open(
        state, session_id, window_seconds=window_seconds
    ):
        return "passive_reply_window_expired"
    used = state.latest_sequence_by_session_and_msg_id.get((session_id, msg_id), 0)
    if used >= replies_per_msg_id:
        return "passive_reply_limit_reached"
    return None


def _record_pending_audit(
    state: QQSessionState,
    *,
    record_key: tuple[str, str, str],
    content: str,
    content_hash: str,
    msg_seq: int,
    session_id: str,
    target_openid: str,
    error_code: int | None,
) -> dict[str, object]:
    # 304023/304024: the platform accepted the message and queued it for
    # manual review — record it as sent so retries do not duplicate it.
    result: dict[str, object] = {"status": "pending_audit"}
    state.send_records[record_key] = QQSendRecord(
        content=content,
        content_hash=content_hash,
        msg_seq=msg_seq,
        result=dict(result),
    )
    logger.info(
        "qq.send pending_audit session_id={} openid={} code={}",
        session_id,
        target_openid,
        error_code,
    )
    return result


def next_msg_seq(state: QQSessionState, session_id: str, msg_id: str) -> int:
    key = (session_id, msg_id)
    current = state.latest_sequence_by_session_and_msg_id.get(key, 0) + 1
    state.latest_sequence_by_session_and_msg_id[key] = current
    return current


def build_already_sent_result(send_record: QQSendRecord) -> dict[str, object]:
    result = dict(send_record.result)
    result["status"] = "already_sent"
    return result


def hash_outbound_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def normalize_outbound_content(content: str) -> str:
    normalized = content.strip()
    normalized = re.sub(r"^\$qq\s*→\s*", "", normalized, count=1, flags=re.IGNORECASE)
    return _strip_edge_special_tokens(normalized)


def is_passive_reply_window_open(
    state: QQSessionState,
    session_id: str,
    *,
    window_seconds: float = DEFAULT_PASSIVE_REPLY_WINDOW_SECONDS,
) -> bool:
    # Fail open on a missing or unparsable timestamp: blocking the reply
    # locally would be worse than letting QQ reject an expired one, and QQ
    # events are not guaranteed to carry a timestamp.
    timestamp = state.latest_timestamp_by_session.get(session_id)
    if not timestamp:
        return True
    try:
        sent_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return True
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return datetime.now(sent_at.tzinfo) - sent_at <= timedelta(seconds=window_seconds)
