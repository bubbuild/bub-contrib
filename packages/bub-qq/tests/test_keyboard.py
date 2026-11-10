from __future__ import annotations

import asyncio
import json

from bub.channels.message import ChannelMessage

from bub import configure
from bub_qq.channel import QQChannel
from bub_qq.inbound.interaction import INTERACTION_BUTTON
from bub_qq.inbound.interaction import build_interaction_channel_message
from bub_qq.inbound.interaction import parse_interaction_event
from bub_qq.outbound.group import QQGroupSendService
from bub_qq.outbound.media import apply_at_user_tags
from bub_qq.outbound.media import build_outbound_context
from bub_qq.session import QQSessionState


_CONFIRM_KEYBOARD = {
    "content": {
        "rows": [
            {
                "buttons": [
                    {
                        "id": "allow-once",
                        "render_data": {"label": "确认", "visited_label": "已确认", "style": 1},
                        "action": {
                            "type": 1,
                            "permission": {"type": 2},
                            "data": "confirm:once",
                        },
                    }
                ]
            }
        ]
    }
}


class GroupKeyboardOpenAPI:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post_group_text_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"id": "kb-1"}

    async def post_group_markdown_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append({"op": "markdown", **kwargs})
        return {"id": "kb-md"}


def test_apply_at_user_tags_prefixes_official_chip() -> None:
    tagged = apply_at_user_tags("你好", ["ABC123"])
    assert tagged == '<qqbot-at-user id="ABC123" /> 你好'
    assert apply_at_user_tags(tagged, ["ABC123"]) == tagged


def test_group_send_prefixes_at_user_tag() -> None:
    async def _run() -> None:
        state = QQSessionState()
        state.latest_message_id_by_session["qq:group:group-openid"] = "group-message-1"
        state.latest_timestamp_by_session["qq:group:group-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        openapi = GroupKeyboardOpenAPI()
        service = QQGroupSendService(
            channel_name="qq",
            receive_mode="websocket",
            state=state,
            openapi=openapi,
        )
        result = await service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                chat_id="group:group-openid",
                content="请确认",
                channel="qq",
                context=build_outbound_context(at_user_ids=["member-openid"]),
            )
        )
        assert result == {"id": "kb-md"}
        assert openapi.calls[0]["op"] == "markdown"
        assert (
            openapi.calls[0]["content"]
            == '<qqbot-at-user id="member-openid" /> 请确认'
        )

    asyncio.run(_run())


def test_group_send_attaches_keyboard_to_text() -> None:
    async def _run() -> None:
        state = QQSessionState()
        state.latest_message_id_by_session["qq:group:group-openid"] = "group-message-1"
        state.latest_timestamp_by_session["qq:group:group-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        openapi = GroupKeyboardOpenAPI()
        service = QQGroupSendService(
            channel_name="qq",
            receive_mode="websocket",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                chat_id="group:group-openid",
                content="请确认",
                channel="qq",
                context=build_outbound_context(keyboard=_CONFIRM_KEYBOARD),
            )
        )

        assert result == {"id": "kb-md"}
        assert openapi.calls[0]["op"] == "markdown"
        assert openapi.calls[0]["keyboard"] == _CONFIRM_KEYBOARD
        assert openapi.calls[0]["content"] == "请确认"

    asyncio.run(_run())


def test_parse_interaction_button_event() -> None:
    event = parse_interaction_event(
        {
            "op": 0,
            "t": "INTERACTION_CREATE",
            "d": {
                "id": "1b13d569-4610-4ab9-bc51-feecc5def6d4",
                "type": 11,
                "scene": "group",
                "chat_type": 1,
                "group_openid": "group-openid",
                "group_member_openid": "member-openid",
                "timestamp": "2099-01-01T00:00:00+00:00",
                "data": {
                    "type": 11,
                    "resolved": {
                        "button_data": "confirm:once",
                        "button_id": "allow-once",
                    },
                },
            },
        }
    )

    assert event is not None
    assert event["type"] == INTERACTION_BUTTON
    assert event["group_openid"] == "group-openid"
    assert event["group_member_openid"] == "member-openid"
    message = build_interaction_channel_message("qq", event)
    assert message is not None
    payload = json.loads(message.content)
    assert payload["type"] == "interaction"
    assert payload["button_id"] == "allow-once"
    assert payload["button_data"] == "confirm:once"
    assert payload["sender_id"] == "member-openid"
    assert message.session_id == "qq:group:group-openid"


def test_channel_acks_button_then_receives(tmp_path) -> None:
    async def _run() -> None:
        received: list[ChannelMessage] = []

        async def on_receive(message: ChannelMessage) -> None:
            received.append(message)

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
        channel = QQChannel(on_receive)
        acks: list[dict[str, object]] = []

        class _Ack:
            async def put_interaction(
                self,
                *,
                interaction_id: str,
                code: int = 0,
                data: dict[str, object] | None = None,
            ) -> dict[str, object]:
                acks.append({"id": interaction_id, "code": code, "data": data})
                return {}

        channel._openapi = _Ack()  # type: ignore[assignment]
        await channel._handle_transport_payload(
            {
                "op": 0,
                "t": "INTERACTION_CREATE",
                "d": {
                    "id": "interaction-btn",
                    "type": 11,
                    "scene": "c2c",
                    "user_openid": "user-openid",
                    "timestamp": "2099-01-01T00:00:00+00:00",
                    "data": {
                        "type": 11,
                        "resolved": {
                            "button_id": "allow-once",
                            "button_data": "confirm:once",
                        },
                    },
                },
            }
        )

        assert acks == [{"id": "interaction-btn", "code": 0, "data": None}]
        assert len(received) == 1
        payload = json.loads(received[0].content)
        assert payload["button_id"] == "allow-once"
        assert (
            channel._session_state.latest_message_id_by_session["qq:c2c:user-openid"]
            == "event:interaction-btn"
        )

    asyncio.run(_run())
    configure._global_config.clear()
    configure._config_data.clear()
