from __future__ import annotations

import asyncio

from bub import configure
from bub.channels.message import ChannelMessage

from bub_qq.channel import QQChannel
from bub_qq.inbound.c2c import build_c2c_channel_message
from bub_qq.outbound.c2c import QQC2CSendService
from bub_qq.outbound.group import QQGroupSendService
from bub_qq.protocol.errors import QQKnownOpenAPIError
from bub_qq.protocol.errors import QQOpenAPIError
from bub_qq.protocol.models import QQC2CMessage


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

    async def post_c2c_markdown_message(
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
                "msg_type": 2,
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

    async def post_c2c_markdown_message(
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


class GroupOpenAPIStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post_group_text_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "group_openid": group_openid,
                "content": content,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "group-reply-1"}

    async def post_group_markdown_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "group_openid": group_openid,
                "content": content,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
                "msg_type": 2,
            }
        )
        return {"id": "group-reply-1"}


def _install_send_service(channel: QQChannel, openapi: object) -> None:
    channel._c2c_send = QQC2CSendService(  # type: ignore[assignment]
        channel_name=channel.name,
        receive_mode=channel._config.receive_mode,
        state=channel._session_state,
        openapi=openapi,  # type: ignore[arg-type]
    )


def test_channel_send_uses_latest_c2c_message_context() -> None:
    async def _run() -> None:
        configure.merge(configure._config_data, {"qq": {"receive_mode": "webhook"}})
        configure._global_config.clear()
        channel = QQChannel(lambda message: None)
        openapi = OpenAPIStub()
        _install_send_service(channel, openapi)
        channel._session_state.latest_message_id_by_session["qq:c2c:user-openid"] = (
            "message-1"
        )
        channel._session_state.latest_timestamp_by_session["qq:c2c:user-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )

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
                known=QQKnownOpenAPIError(
                    304027, "MSG_EXPIRE", "回复的消息过期", "reply", False
                ),
            )
        )
        _install_send_service(channel, openapi)
        channel._session_state.latest_message_id_by_session["qq:c2c:user-openid"] = (
            "message-1"
        )
        channel._session_state.latest_timestamp_by_session["qq:c2c:user-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )

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
                known=QQKnownOpenAPIError(
                    22009, "MsgLimitExceed", "消息发送超频", "rate_limit", True
                ),
            )
        )
        _install_send_service(channel, openapi)
        channel._session_state.latest_message_id_by_session["qq:c2c:user-openid"] = (
            "message-1"
        )
        channel._session_state.latest_timestamp_by_session["qq:c2c:user-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )

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


def test_channel_handles_group_at_message() -> None:
    async def _run() -> None:
        received: list[ChannelMessage] = []

        async def handler(message: ChannelMessage) -> None:
            received.append(message)

        configure.merge(configure._config_data, {"qq": {"receive_mode": "webhook"}})
        configure._global_config.clear()
        channel = QQChannel(handler)

        await channel._handle_transport_payload(
            {
                "id": "event-1",
                "op": 0,
                "s": 1,
                "t": "GROUP_AT_MESSAGE_CREATE",
                "d": {
                    "author": {
                        "member_openid": "member-openid",
                        "username": "Alice",
                    },
                    "content": "<@bot-openid> ping",
                    "id": "group-message-1",
                    "group_openid": "group-openid",
                    "timestamp": "2099-01-01T00:00:00+00:00",
                    "mentions": [
                        {
                            "member_openid": "bot-openid",
                            "is_you": True,
                        }
                    ],
                },
            }
        )

        assert len(received) == 1
        assert received[0].session_id == "qq:group:group-openid"
        assert received[0].is_active is True

    asyncio.run(_run())
    configure._global_config.clear()
    configure._config_data.clear()


class InteractionOpenAPIStub:
    def __init__(self) -> None:
        self.acks: list[dict[str, object]] = []

    async def put_interaction(
        self,
        *,
        interaction_id: str,
        code: int = 0,
        data: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.acks.append({"id": interaction_id, "code": code, "data": data})
        return {}


def test_channel_records_msg_toggle_events(tmp_path) -> None:
    async def _run() -> None:
        configure.merge(
            configure._config_data,
            {
                "qq": {
                    "receive_mode": "webhook",
                    "state_file": str(tmp_path / "state.json"),
                }
            },
        )
        configure._global_config.clear()
        channel = QQChannel(lambda message: None)

        await channel._handle_transport_payload(
            {"op": 0, "t": "GROUP_MSG_RECEIVE", "d": {"group_openid": "group-1"}}
        )
        await channel._handle_transport_payload(
            {"op": 0, "t": "GROUP_MSG_REJECT", "d": {"group_openid": "group-2"}}
        )
        await channel._handle_transport_payload(
            {"op": 0, "t": "C2C_MSG_RECEIVE", "d": {"openid": "user-1"}}
        )
        await channel._handle_transport_payload(
            {"op": 0, "t": "C2C_MSG_REJECT", "d": {"openid": "user-2"}}
        )

        store = channel._platform_store
        assert store.active_messages_allowed("group", "group-1") is True
        assert store.active_messages_allowed("group", "group-2") is False
        assert store.active_messages_allowed("c2c", "user-1") is True
        assert store.active_messages_allowed("c2c", "user-2") is False

    asyncio.run(_run())
    configure._global_config.clear()
    configure._config_data.clear()


def test_channel_persists_claw_cfg_update_and_echoes_state(tmp_path) -> None:
    async def _run() -> None:
        configure.merge(
            configure._config_data,
            {
                "qq": {
                    "receive_mode": "webhook",
                    "state_file": str(tmp_path / "state.json"),
                }
            },
        )
        configure._global_config.clear()
        channel = QQChannel(lambda message: None)
        openapi = InteractionOpenAPIStub()
        channel._openapi = openapi  # type: ignore[assignment]

        await channel._handle_transport_payload(
            {
                "op": 0,
                "t": "INTERACTION_CREATE",
                "d": {
                    "id": "interaction-1",
                    "group_openid": "group-1",
                    "data": {
                        "type": 2002,
                        "resolved": {"claw_cfg": {"require_mention": "mention"}},
                    },
                },
            }
        )

        assert channel._platform_store.require_mention("group-1") == "mention"
        first_ack = openapi.acks[0]
        assert first_ack["data"]["claw_cfg"]["require_mention"] == "mention"  # type: ignore[index]

        await channel._handle_transport_payload(
            {
                "op": 0,
                "t": "INTERACTION_CREATE",
                "d": {
                    "id": "interaction-2",
                    "group_openid": "group-1",
                    "data": {"type": 2001, "resolved": {}},
                },
            }
        )

        second_ack = openapi.acks[1]
        assert second_ack["data"]["claw_cfg"]["require_mention"] == "mention"  # type: ignore[index]

    asyncio.run(_run())
    configure._global_config.clear()
    configure._config_data.clear()


def test_channel_send_routes_group_messages() -> None:
    async def _run() -> None:
        configure.merge(configure._config_data, {"qq": {"receive_mode": "webhook"}})
        configure._global_config.clear()
        channel = QQChannel(lambda message: None)
        openapi = GroupOpenAPIStub()
        channel._group_send = QQGroupSendService(  # type: ignore[assignment]
            channel_name=channel.name,
            receive_mode=channel._config.receive_mode,
            state=channel._session_state,
            openapi=openapi,
        )
        channel._session_state.latest_message_id_by_session["qq:group:group-openid"] = (
            "group-message-1"
        )
        channel._session_state.latest_timestamp_by_session["qq:group:group-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )

        await channel.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                content="hello group",
                channel="qq",
                chat_id="group:group-openid",
            )
        )

        assert openapi.calls == [
            {
                "group_openid": "group-openid",
                "content": "hello group",
                "msg_id": "group-message-1",
                "msg_seq": 1,
            }
        ]

    asyncio.run(_run())
    configure._global_config.clear()
    configure._config_data.clear()
