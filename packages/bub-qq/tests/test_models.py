from __future__ import annotations

from bub_qq.protocol.models import QQC2CMessage
from bub_qq.protocol.models import QQGroupMessage


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


def test_group_message_parses_at_event() -> None:
    message = QQGroupMessage.from_event(
        {
            "id": "event-2",
            "op": 0,
            "s": 7,
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "author": {
                    "member_openid": "member-openid",
                    "username": "Alice",
                    "member_role": "owner",
                },
                "content": "<@bot-openid> hello",
                "id": "group-message-1",
                "group_openid": "group-openid",
                "timestamp": "2023-11-06T13:37:18+08:00",
                "mentions": [
                    {
                        "member_openid": "bot-openid",
                        "nickname": "Bot",
                        "is_you": True,
                    }
                ],
            },
        }
    )

    assert message.event_id == "event-2"
    assert message.sequence == 7
    assert message.group_openid == "group-openid"
    assert message.member_openid == "member-openid"
    assert message.sender_name == "Alice"
    assert message.message_id == "group-message-1"
    assert message.event_type == "GROUP_AT_MESSAGE_CREATE"
    assert message.mentions[0].is_you is True
    assert message.attachments == ()
    assert message.member_role == "owner"


def test_c2c_message_parses_quoted_msg_elements() -> None:
    message = QQC2CMessage.from_event(
        {
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "author": {"user_openid": "user-openid"},
                "content": "这个建议很有帮助，谢谢你！",
                "id": "message-quote-1",
                "message_type": 103,
                "msg_elements": [
                    {
                        "msg_idx": "REFIDX_aaa==",
                        "message_type": 103,
                        "content": "每天坚持阅读半小时",
                        "author": {"username": "Bob"},
                        "msg_elements": [
                            {"message_type": 0, "content": "nested record"}
                        ],
                    }
                ],
            },
        }
    )

    assert message.message_type == 103
    assert len(message.msg_elements) == 1
    element = message.msg_elements[0]
    assert element.content == "每天坚持阅读半小时"
    assert element.sender_name == "Bob"
    assert element.elements[0].content == "nested record"


def test_group_message_member_role_defaults_to_none() -> None:
    message = QQGroupMessage.from_event(
        {
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "author": {"member_openid": "member-openid"},
                "content": "hello",
                "id": "group-message-2",
                "group_openid": "group-openid",
            },
        }
    )

    assert message.member_role is None
