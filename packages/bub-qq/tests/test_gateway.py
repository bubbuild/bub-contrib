from __future__ import annotations

from bub_qq.gateway import heartbeat_payload
from bub_qq.gateway import identify_payload
from bub_qq.gateway import resume_payload
from bub_qq.ws_errors import QQWebSocketFatalError
from bub_qq.ws_errors import raise_for_close_code


def test_identify_payload_uses_qqbot_token_and_intents() -> None:
    payload = identify_payload(token="abc", intents=1 << 25, shard=(0, 1))

    assert payload["op"] == 2
    assert payload["d"]["token"] == "QQBot abc"
    assert payload["d"]["intents"] == 1 << 25
    assert payload["d"]["shard"] == [0, 1]


def test_heartbeat_payload_uses_latest_sequence() -> None:
    assert heartbeat_payload(42) == {"op": 1, "d": 42}


def test_resume_payload_uses_session_and_sequence() -> None:
    payload = resume_payload(token="abc", session_id="session-1", sequence=42)

    assert payload == {
        "op": 6,
        "d": {
            "token": "QQBot abc",
            "session_id": "session-1",
            "seq": 42,
        },
    }


def test_websocket_fatal_close_code_stops_reconnect() -> None:
    try:
        raise_for_close_code(4915)
    except QQWebSocketFatalError as exc:
        assert exc.code == 4915
        assert "banned" in str(exc)
    else:
        raise AssertionError("expected fatal websocket close code")
