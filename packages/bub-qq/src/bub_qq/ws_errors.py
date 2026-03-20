from __future__ import annotations


RETRYABLE_IDENTIFY_CODES = {4006, 4007, 4008, 4009, *range(4900, 4914)}
FATAL_WEBSOCKET_CODES = {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}


class QQWebSocketFatalError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def raise_for_close_code(close_code: int | None) -> None:
    if close_code is None or close_code < 4000:
        return
    if close_code in FATAL_WEBSOCKET_CODES:
        raise QQWebSocketFatalError(close_code, close_code_message(close_code))
    if close_code in RETRYABLE_IDENTIFY_CODES:
        raise RuntimeError(
            f"qq websocket reconnect required code={close_code} {close_code_message(close_code)}"
        )
    raise RuntimeError(f"qq websocket closed code={close_code} {close_code_message(close_code)}")


def close_code_message(code: int) -> str:
    messages = {
        4001: "invalid opcode",
        4002: "invalid payload",
        4006: "invalid session id, identify required",
        4007: "invalid seq, identify required",
        4008: "payload sent too fast, reconnect and identify again",
        4009: "session expired, reconnect and resume or identify",
        4010: "invalid shard",
        4011: "too many guilds for this connection",
        4012: "invalid version",
        4013: "invalid intent",
        4014: "intent not permitted",
        4914: "bot limited to sandbox environment only",
        4915: "bot is banned",
    }
    if 4900 <= code <= 4913:
        return "internal error, reconnect and identify again"
    return messages.get(code, "unknown websocket close code")
