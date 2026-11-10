from __future__ import annotations

import json
from typing import Any

from bub.channels.message import ChannelMessage
from loguru import logger

from ..protocol.models import QQC2CMessage
from ..guard import Requester
from ..security import QQ_CONTEXT_KEY
from ..admins import is_admin_command
from ..security import strip_command
from .common import attachment_payloads
from .common import exclude_none
from .common import QQInboundService
from .common import msg_element_payloads
from .common import resolve_scoped_openid


class QQC2CInboundService(QQInboundService[QQC2CMessage]):
    scope = "c2c"

    def _parse(self, payload: dict[str, Any]) -> QQC2CMessage:
        return QQC2CMessage.from_event(payload)

    def _requester(self, message: QQC2CMessage) -> Requester:
        return Requester(scope="c2c", sender_id=message.user_openid)

    def _allowed(self, message: QQC2CMessage, requester: Requester) -> bool:
        # Admins are let through even when they are not on allow_users.
        if self._policy.user_allowed(message.user_openid) or self._is_admin(requester):
            return True
        logger.warning(
            "qq.c2c.blocked user_openid={} reason=not_in_allow_users",
            message.user_openid,
        )
        return False

    def _build(self, message: QQC2CMessage, *, allow_command: bool) -> ChannelMessage:
        return build_c2c_channel_message(
            self._channel_name,
            message,
            allow_command=allow_command,
            suppress_direct_output=self._suppress_direct_output,
        )


def build_c2c_channel_message(
    channel_name: str,
    message: QQC2CMessage,
    *,
    allow_command: bool = False,
    suppress_direct_output: bool = False,
) -> ChannelMessage:
    session_id = f"{channel_name}:c2c:{message.user_openid}"
    chat_id = f"c2c:{message.user_openid}"
    text = message.content.strip()
    context = {
        QQ_CONTEXT_KEY: {
            "scope": "c2c",
            "sender_id": message.user_openid,
            "message_id": message.message_id,
        }
    }

    if strip_command(text) is not None:
        if allow_command:
            return ChannelMessage(
                session_id=session_id,
                content=text,
                channel=channel_name,
                chat_id=chat_id,
                kind="command",
                is_active=True,
                context=context,
            )
        if not is_admin_command(text):
            logger.warning(
                "qq.c2c.command_denied user_openid={} reason=not_admin_user",
                message.user_openid,
            )

    payload = {
        "message": text,
        "message_id": message.message_id,
        "type": "text" if not message.attachments else "attachment",
        "sender_id": message.user_openid,
        "date": message.timestamp,
        "attachments": attachment_payloads(message.attachments),
        "quoted_messages": msg_element_payloads(
            message.msg_elements, first_index=len(message.attachments)
        ),
        "message_type": message.message_type,
        "ark_data": message.ark_data,
    }
    return ChannelMessage(
        session_id=session_id,
        content=json.dumps(exclude_none(payload), ensure_ascii=False),
        channel=channel_name,
        chat_id=chat_id,
        is_active=True,
        context=context,
        # In tool reply mode the model replies via the qq.send tool; route
        # the direct model output to the "null" channel so it is dropped.
        # Command results (above) always stay on the direct route.
        output_channel="null" if suppress_direct_output else "",
    )


def resolve_c2c_openid(
    *, channel_name: str, session_id: str, chat_id: str
) -> str | None:
    return resolve_scoped_openid(
        "c2c", channel_name=channel_name, session_id=session_id, chat_id=chat_id
    )
