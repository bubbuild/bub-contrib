from __future__ import annotations

from loguru import logger

from .openapi_errors import QQOpenAPIError


def is_duplicate_send_error(exc: QQOpenAPIError) -> bool:
    return exc.error_code == 40054005


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


def log_send_error(
    exc: QQOpenAPIError,
    *,
    session_id: str,
    openid: str,
    msg_id: str,
    msg_seq: int,
    receive_mode: str,
) -> None:
    code = exc.error_code
    trace_id = exc.trace_id or "-"
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
    if code == 304027:
        logger.warning(
            "qq.send failed session_id={} openid={} msg_id={} msg_seq={} reason=reply_expired trace_id={}",
            session_id,
            openid,
            msg_id,
            msg_seq,
            trace_id,
        )
        return
    if code == 304031:
        logger.warning(
            "qq.send failed session_id={} openid={} reason=dm_closed trace_id={}",
            session_id,
            openid,
            trace_id,
        )
        return
    if code in {22009, 20028, 304045, 304049, 1100100, 1100308}:
        logger.warning(
            "qq.send failed session_id={} openid={} msg_id={} msg_seq={} reason=rate_limited code={} retryable={} trace_id={}",
            session_id,
            openid,
            msg_id,
            msg_seq,
            code,
            exc.known.retryable if exc.known is not None else False,
            trace_id,
        )
        return
    if code == 304018:
        logger.warning(
            "qq.send failed session_id={} openid={} reason=gateway_session_missing receive_mode={} trace_id={}",
            session_id,
            openid,
            receive_mode,
            trace_id,
        )
        return
    if code in {304026, 50048}:
        logger.warning(
            "qq.send failed session_id={} openid={} reason=invalid_reply_message_id msg_id={} msg_seq={} trace_id={}",
            session_id,
            openid,
            msg_id,
            msg_seq,
            trace_id,
        )
        return
    if code in {304028, 50045, 50046, 50047}:
        logger.warning(
            "qq.send failed session_id={} openid={} reason=reply_not_allowed code={} msg_id={} trace_id={}",
            session_id,
            openid,
            code,
            msg_id,
            trace_id,
        )
        return
    if code in {304025, 1100101, 1100102, 1100103}:
        logger.warning(
            "qq.send failed session_id={} openid={} reason=safety_blocked code={} trace_id={}",
            session_id,
            openid,
            code,
            trace_id,
        )
        return
    logger.error(
        "qq.send failed session_id={} openid={} msg_id={} msg_seq={} code={} trace_id={} error={}",
        session_id,
        openid,
        msg_id,
        msg_seq,
        code,
        trace_id,
        exc,
    )
