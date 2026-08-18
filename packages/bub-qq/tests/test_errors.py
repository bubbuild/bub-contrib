from __future__ import annotations

from bub_qq.protocol.errors import build_openapi_error
from bub_qq.protocol.errors import extract_business_code
from bub_qq.protocol.errors import lookup_known_error


class _ResponseStub:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.status = 400
        self.reason = "Bad Request"
        self.headers = headers or {}


def test_extract_business_code_reads_legacy_code_field() -> None:
    assert extract_business_code({"code": 304027}) == 304027


def test_extract_business_code_reads_new_err_code_field() -> None:
    assert extract_business_code({"err_code": 40034005}) == 40034005


def test_extract_business_code_prefers_legacy_code_field() -> None:
    assert extract_business_code({"code": 1, "err_code": 2}) == 1


def test_build_openapi_error_parses_new_response_format() -> None:
    payload = {
        "err_code": 40034005,
        "message": "回复消息msg_id已过期",
        "trace_id": "4a8a61565b909f199b1ec169fdd6f49e",
    }

    error = build_openapi_error(_ResponseStub(), payload)

    assert error.error_code == 40034005
    assert error.error_message == "回复消息msg_id已过期"
    assert error.trace_id == "4a8a61565b909f199b1ec169fdd6f49e"
    assert error.known is not None
    assert error.known.category == "reply"


def test_build_openapi_error_prefers_header_trace_id() -> None:
    payload = {"err_code": 1, "trace_id": "body-trace"}
    response = _ResponseStub(headers={"X-Tps-trace-ID": "header-trace"})

    error = build_openapi_error(response, payload)

    assert error.trace_id == "header-trace"


def test_lookup_known_error_covers_new_reply_expired_code() -> None:
    known = lookup_known_error(40034005)

    assert known is not None
    assert known.name == "ReplyMessageExpired"
