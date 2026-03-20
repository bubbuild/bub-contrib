from __future__ import annotations

from bub_qq.models import QQC2CMessage


def test_c2c_message_parses_minimal_payload() -> None:
    message = QQC2CMessage.from_event(
        {
            "id": "event-1",
            "op": 0,
            "s": 42,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "author": {"user_openid": "user-openid"},
                "content": "123",
                "id": "message-1",
                "timestamp": "2023-11-06T13:37:18+08:00",
            },
        }
    )

    assert message.event_id == "event-1"
    assert message.sequence == 42
    assert message.user_openid == "user-openid"
    assert message.message_id == "message-1"
    assert message.content == "123"
    assert message.timestamp == "2023-11-06T13:37:18+08:00"
    assert message.attachments == ()
