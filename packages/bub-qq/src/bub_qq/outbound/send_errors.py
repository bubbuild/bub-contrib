from __future__ import annotations

from loguru import logger

from ..protocol.errors import QQOpenAPIError


def is_duplicate_send_error(exc: QQOpenAPIError) -> bool:
    return exc.error_code == 40054005


def is_pending_audit_error(exc: QQOpenAPIError) -> bool:
    """Async-accepted codes: the message awaits manual review, not a failure.

    304023 (push) / 304024 (reply) mean the platform accepted the call and
    queued the message for human audit.
    """

    return exc.error_code in {304023, 304024}


def log_send_duplicate_error(
    exc: QQOpenAPIError,
    *,
    session_id: str,
    openid: str,
    msg_id: str,
    msg_seq: int,
    content_hash: str,
) -> None:
    logger.warning(
        "qq.send failed session_id={} openid={} msg_id={} msg_seq={} reason=already_sent source=remote_dedup_hit code={} trace_id={} content_hash={} error={}",
        session_id,
        openid,
        msg_id,
        msg_seq,
        exc.error_code,
        exc.trace_id or "-",
        content_hash,
        exc.error_message,
    )


# One place that names the send failures we recognise. Codes not listed
# fall back to the error catalog's category (see protocol/errors.py).
_REASON_BY_CODE: dict[int, str] = {
    304027: "reply_expired",
    40034005: "reply_expired",
    40034026: "reply_expired",  # event_id expired
    40034128: "reply_expired",  # passive reply window or count exceeded
    304031: "dm_closed",
    22009: "rate_limited",
    20028: "rate_limited",
    304045: "rate_limited",
    304049: "rate_limited",
    1100100: "rate_limited",
    1100308: "rate_limited",
    304018: "gateway_session_missing",
    304026: "invalid_reply_message_id",
    50048: "invalid_reply_message_id",
    40034025: "invalid_reply_message_id",  # invalid event_id
    304028: "reply_not_allowed",
    50045: "reply_not_allowed",
    50046: "reply_not_allowed",
    50047: "reply_not_allowed",
    40034027: "reply_not_allowed",  # event type does not support replies
    304025: "safety_blocked",
    1100101: "safety_blocked",
    1100102: "safety_blocked",
    1100103: "safety_blocked",
}
_REASON_BY_CATEGORY: dict[str, str] = {
    "rate_limit": "rate_limited",
    "safety": "safety_blocked",
}


def send_error_reason(exc: QQOpenAPIError) -> str | None:
    """A short reason for a recognised send failure, else None."""

    if exc.error_code is not None and exc.error_code in _REASON_BY_CODE:
        return _REASON_BY_CODE[exc.error_code]
    if exc.known is not None:
        return _REASON_BY_CATEGORY.get(exc.known.category)
    return None


def log_send_error(
    exc: QQOpenAPIError,
    *,
    session_id: str,
    openid: str,
    msg_id: str,
    msg_seq: int,
    receive_mode: str,
) -> None:
    if is_duplicate_send_error(exc):
        log_send_duplicate_error(
            exc,
            session_id=session_id,
            openid=openid,
            msg_id=msg_id,
            msg_seq=msg_seq,
            content_hash="-",
        )
        return
    reason = send_error_reason(exc)
    fields = (
        "qq.send failed session_id={} openid={} msg_id={} msg_seq={} reason={}"
        " code={} retryable={} receive_mode={} trace_id={}"
    )
    args = (
        session_id,
        openid,
        msg_id,
        msg_seq,
        reason or "unknown",
        exc.error_code,
        exc.known.retryable if exc.known is not None else False,
        receive_mode,
        exc.trace_id or "-",
    )
    if reason is not None:
        logger.warning(fields, *args)
        return
    logger.error(fields + " error={}", *args, exc)
