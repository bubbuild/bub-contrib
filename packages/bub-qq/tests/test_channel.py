from __future__ import annotations

import asyncio

from bub import configure
from bub.channels.message import ChannelMessage

from bub_qq.channel import QQChannel
from bub_qq.c2c import build_c2c_channel_message
from bub_qq.c2c import QQC2CSendService
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


def _install_send_service(channel: QQChannel, openapi: object) -> None:
    channel._c2c_send = QQC2CSendService(  # type: ignore[assignment]
        channel_name=channel.name,
        receive_mode=channel._config.receive_mode,
        state=channel._c2c_state,
        openapi=openapi,  # type: ignore[arg-type]
    )


def test_channel_send_uses_latest_c2c_message_context() -> None:
    async def _run() -> None:
        configure.merge(configure._config_data, {"qq": {"receive_mode": "webhook"}})
        configure._global_config.clear()
        channel = QQChannel(lambda message: None)
        openapi = OpenAPIStub()
        _install_send_service(channel, openapi)
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

        assert openapi.calls == [
            {
                "openid": "user-openid",
                "content": "hello",
                "msg_id": "message-1",
                "msg_seq": 1,
            }
        ]

    asyncio.run(_run())
    configure._global_config.clear()
    configure._config_data.clear()


def test_channel_send_handles_reply_expired_error() -> None:
    async def _run() -> None:
        configure.merge(configure._config_data, {"qq": {"receive_mode": "webhook"}})
        configure._global_config.clear()
        channel = QQChannel(lambda message: None)
        openapi = FailingOpenAPIStub(
            QQOpenAPIError(
                status_code=400,
                trace_id="trace-1",
                error_code=304027,
                error_message="reply expired",
                known=QQKnownOpenAPIError(304027, "MSG_EXPIRE", "回复的消息过期", "reply", False),
            )
        )
        _install_send_service(channel, openapi)
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

        assert openapi.calls == 1

    asyncio.run(_run())
    configure._global_config.clear()
    configure._config_data.clear()


def test_channel_send_handles_rate_limit_error() -> None:
    async def _run() -> None:
        configure.merge(configure._config_data, {"qq": {"receive_mode": "webhook"}})
        configure._global_config.clear()
        channel = QQChannel(lambda message: None)
        openapi = FailingOpenAPIStub(
            QQOpenAPIError(
                status_code=429,
                trace_id="trace-2",
                error_code=22009,
                error_message="msg limit exceed",
                known=QQKnownOpenAPIError(22009, "MsgLimitExceed", "消息发送超频", "rate_limit", True),
            )
        )
        _install_send_service(channel, openapi)
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

        assert openapi.calls == 1

    asyncio.run(_run())
    configure._global_config.clear()
    configure._config_data.clear()


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

    assert channel_message.output_channel != "null"
