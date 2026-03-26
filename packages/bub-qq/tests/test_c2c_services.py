from __future__ import annotations

import asyncio

from bub.channels.message import ChannelMessage

from bub_qq.c2c import QQC2CDeduper
from bub_qq.c2c import QQC2CInboundService
from bub_qq.c2c import QQC2CSendService
from bub_qq.c2c import QQC2CSessionState
from bub_qq.openapi_errors import QQKnownOpenAPIError
from bub_qq.openapi_errors import QQOpenAPIError


class OpenAPIStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post_c2c_text_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "openid": openid,
                "content": content,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "reply-1"}


class FailingOpenAPIStub:
    def __init__(self, error: QQOpenAPIError) -> None:
        self.error = error
        self.calls = 0

    async def post_c2c_text_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        del openid, content, msg_id, msg_seq
        self.calls += 1
        raise self.error


def _state() -> QQC2CSessionState:
    return QQC2CSessionState(
        latest_message_id_by_session={},
        latest_sequence_by_session_and_msg_id={},
        latest_timestamp_by_session={},
        send_record_by_session_msg_id_and_seq={},
    )


def _payload(message_id: str = "message-1") -> dict[str, object]:
    return {
        "id": "event-1",
        "op": 0,
        "s": 42,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "author": {"user_openid": "user-openid"},
            "content": "hello",
            "id": message_id,
            "timestamp": "2099-01-01T00:00:00+00:00",
        },
    }


def test_c2c_inbound_service_parses_and_remembers_session() -> None:
    state = _state()
    service = QQC2CInboundService(channel_name="qq", deduper=QQC2CDeduper(16), state=state)

    parsed = service.parse_inbound(_payload())

    assert parsed is not None
    message, channel_message = parsed
    assert message.message_id == "message-1"
    assert channel_message.session_id == "qq:c2c:user-openid"
    assert state.latest_message_id_by_session["qq:c2c:user-openid"] == "message-1"
    assert state.latest_sequence_by_session_and_msg_id == {}


def test_c2c_inbound_service_dedupes_repeated_messages() -> None:
    state = _state()
    service = QQC2CInboundService(channel_name="qq", deduper=QQC2CDeduper(16), state=state)

    assert service.parse_inbound(_payload("message-1")) is not None
    assert service.parse_inbound(_payload("message-1")) is None


def test_c2c_send_service_sends_using_session_context() -> None:
    async def _run() -> None:
        state = _state()
        state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        state.latest_timestamp_by_session["qq:c2c:user-openid"] = "2099-01-01T00:00:00+00:00"
        openapi = OpenAPIStub()
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="hello",
                channel="qq",
            )
        )

        assert result == {"id": "reply-1"}
        assert openapi.calls == [
            {
                "openid": "user-openid",
                "content": "hello",
                "msg_id": "message-1",
                "msg_seq": 1,
            }
        ]

    asyncio.run(_run())


def test_c2c_send_service_starts_msg_seq_at_one_after_inbound_transport_sequence() -> None:
    async def _run() -> None:
        state = _state()
        inbound = QQC2CInboundService(channel_name="qq", deduper=QQC2CDeduper(16), state=state)
        parsed = inbound.parse_inbound(_payload())

        assert parsed is not None

        openapi = OpenAPIStub()
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="hello",
                channel="qq",
            )
        )

        assert result == {"id": "reply-1"}
        assert openapi.calls == [
            {
                "openid": "user-openid",
                "content": "hello",
                "msg_id": "message-1",
                "msg_seq": 1,
            }
        ]
        assert state.latest_sequence_by_session_and_msg_id[
            ("qq:c2c:user-openid", "message-1")
        ] == 1

    asyncio.run(_run())


def test_c2c_send_service_resets_msg_seq_for_new_inbound_msg_id() -> None:
    async def _run() -> None:
        state = _state()
        openapi = OpenAPIStub()
        inbound = QQC2CInboundService(channel_name="qq", deduper=QQC2CDeduper(16), state=state)
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        assert inbound.parse_inbound(_payload("message-1")) is not None
        first = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="first reply",
                channel="qq",
            )
        )
        second = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="second reply",
                channel="qq",
            )
        )

        assert inbound.parse_inbound(_payload("message-2")) is not None
        third = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="reply for new inbound message",
                channel="qq",
            )
        )

        assert first == {"id": "reply-1"}
        assert second == {"id": "reply-1"}
        assert third == {"id": "reply-1"}
        assert openapi.calls == [
            {
                "openid": "user-openid",
                "content": "first reply",
                "msg_id": "message-1",
                "msg_seq": 1,
            },
            {
                "openid": "user-openid",
                "content": "second reply",
                "msg_id": "message-1",
                "msg_seq": 2,
            },
            {
                "openid": "user-openid",
                "content": "reply for new inbound message",
                "msg_id": "message-2",
                "msg_seq": 1,
            },
        ]

    asyncio.run(_run())


def test_c2c_send_service_strips_qq_wrapper_prefix_before_sending() -> None:
    async def _run() -> None:
        state = _state()
        state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        state.latest_timestamp_by_session["qq:c2c:user-openid"] = "2099-01-01T00:00:00+00:00"
        openapi = OpenAPIStub()
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="$qq → \n你好呀！",
                channel="qq",
            )
        )

        assert result == {"id": "reply-1"}
        assert openapi.calls == [
            {
                "openid": "user-openid",
                "content": "你好呀！",
                "msg_id": "message-1",
                "msg_seq": 1,
            }
        ]

    asyncio.run(_run())


def test_c2c_send_service_allows_multiple_replies_for_same_msg_id() -> None:
    async def _run() -> None:
        state = _state()
        state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        state.latest_timestamp_by_session["qq:c2c:user-openid"] = "2099-01-01T00:00:00+00:00"
        openapi = OpenAPIStub()
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        first = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="hello",
                channel="qq",
            )
        )
        second = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="job finished",
                channel="qq",
            )
        )

        assert first == {"id": "reply-1"}
        assert second == {"id": "reply-1"}
        assert openapi.calls == [
            {
                "openid": "user-openid",
                "content": "hello",
                "msg_id": "message-1",
                "msg_seq": 1,
            },
            {
                "openid": "user-openid",
                "content": "job finished",
                "msg_id": "message-1",
                "msg_seq": 2,
            }
        ]

    asyncio.run(_run())


def test_c2c_send_service_skips_when_passive_reply_window_expired() -> None:
    async def _run() -> None:
        state = _state()
        state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        state.latest_timestamp_by_session["qq:c2c:user-openid"] = "2000-01-01T00:00:00+00:00"
        openapi = OpenAPIStub()
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="hello",
                channel="qq",
            )
        )

        assert result is None
        assert openapi.calls == []

    asyncio.run(_run())


def test_c2c_send_service_swallows_openapi_errors() -> None:
    async def _run() -> None:
        state = _state()
        state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        state.latest_timestamp_by_session["qq:c2c:user-openid"] = "2099-01-01T00:00:00+00:00"
        openapi = FailingOpenAPIStub(
            QQOpenAPIError(
                status_code=429,
                trace_id="trace-1",
                error_code=22009,
                error_message="msg limit exceed",
                known=QQKnownOpenAPIError(22009, "MsgLimitExceed", "消息发送超频", "rate_limit", True),
            )
        )
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="hello",
                channel="qq",
            )
        )

        assert result is None
        assert openapi.calls == 1

    asyncio.run(_run())


def test_c2c_send_service_treats_remote_duplicate_as_already_sent() -> None:
    async def _run() -> None:
        state = _state()
        state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        state.latest_timestamp_by_session["qq:c2c:user-openid"] = "2099-01-01T00:00:00+00:00"
        openapi = FailingOpenAPIStub(
            QQOpenAPIError(
                status_code=400,
                trace_id="trace-duplicate",
                error_code=40054005,
                error_message="消息被去重，请检查请求msgseq",
                known=QQKnownOpenAPIError(
                    40054005,
                    "MessageDeduplicated",
                    "消息被去重，请检查请求 msgseq",
                    "reply",
                    False,
                ),
            )
        )
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        first = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="hello",
                channel="qq",
            )
        )
        second = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="hello",
                channel="qq",
            )
        )

        assert first == {"status": "already_sent"}
        assert second == {"status": "already_sent"}
        assert openapi.calls == 2

    asyncio.run(_run())
