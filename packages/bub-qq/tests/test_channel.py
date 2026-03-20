from __future__ import annotations

import asyncio

from bub.channels.message import ChannelMessage

from bub_qq.channel import QQChannel
from bub_qq.c2c import build_c2c_channel_message
from bub_qq.models import QQC2CMessage
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
        return {"id": "reply-1", "timestamp": 123}

    async def aclose(self) -> None:
        return None


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

    async def aclose(self) -> None:
        return None


def test_channel_send_uses_latest_c2c_message_context() -> None:
    async def _run() -> None:
        channel = QQChannel(lambda message: None)
        channel._openapi = OpenAPIStub()  # type: ignore[assignment]
        channel._c2c_state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        channel._c2c_state.latest_timestamp_by_session["qq:c2c:user-openid"] = "2099-01-01T00:00:00+00:00"

        await channel.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                content="hello",
                channel="qq",
                chat_id="c2c:user-openid",
            )
        )

        assert channel._openapi.calls == [  # type: ignore[attr-defined]
            {
                "openid": "user-openid",
                "content": "hello",
                "msg_id": "message-1",
                "msg_seq": 1,
            }
        ]

    asyncio.run(_run())


def test_channel_send_handles_reply_expired_error() -> None:
    async def _run() -> None:
        channel = QQChannel(lambda message: None)
        channel._openapi = FailingOpenAPIStub(  # type: ignore[assignment]
            QQOpenAPIError(
                status_code=400,
                trace_id="trace-1",
                error_code=304027,
                error_message="reply expired",
                known=QQKnownOpenAPIError(304027, "MSG_EXPIRE", "回复的消息过期", "reply", False),
            )
        )
        channel._c2c_state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        channel._c2c_state.latest_timestamp_by_session["qq:c2c:user-openid"] = "2099-01-01T00:00:00+00:00"

        await channel.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                content="hello",
                channel="qq",
                chat_id="c2c:user-openid",
            )
        )

        assert channel._openapi.calls == 1  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_channel_send_handles_rate_limit_error() -> None:
    async def _run() -> None:
        channel = QQChannel(lambda message: None)
        channel._openapi = FailingOpenAPIStub(  # type: ignore[assignment]
            QQOpenAPIError(
                status_code=429,
                trace_id="trace-2",
                error_code=22009,
                error_message="msg limit exceed",
                known=QQKnownOpenAPIError(22009, "MsgLimitExceed", "消息发送超频", "rate_limit", True),
            )
        )
        channel._c2c_state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        channel._c2c_state.latest_timestamp_by_session["qq:c2c:user-openid"] = "2099-01-01T00:00:00+00:00"

        await channel.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                content="hello",
                channel="qq",
                chat_id="c2c:user-openid",
            )
        )

        assert channel._openapi.calls == 1  # type: ignore[attr-defined]

    asyncio.run(_run())


def test_c2c_inbound_defaults_outbound_to_qq_channel() -> None:
    message = QQC2CMessage(
        message_id="message-1",
        event_id="event-1",
        user_openid="user-openid",
        content="hello",
        timestamp="2026-03-19T00:00:00+00:00",
        attachments=(),
        sequence=1,
    )

    channel_message = build_c2c_channel_message("qq", message)

    assert channel_message.output_channel == "null"
