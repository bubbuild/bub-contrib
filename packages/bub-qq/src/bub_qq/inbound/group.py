from __future__ import annotations

import json
import re
from typing import Any

from bub.channels.message import ChannelMessage
from loguru import logger

from ..protocol.models import QQGroupMessage
from ..protocol.models import QQMention
from ..guard import Requester
from ..security import QQ_CONTEXT_KEY
from ..admins import is_admin_command
from ..security import strip_command
from .common import attachment_payloads
from .common import exclude_none
from .common import QQInboundService
from .common import msg_element_payloads
from .common import resolve_scoped_openid

GROUP_AT_EVENT = "GROUP_AT_MESSAGE_CREATE"
GROUP_MESSAGE_EVENT = "GROUP_MESSAGE_CREATE"
GROUP_EVENTS = {GROUP_AT_EVENT, GROUP_MESSAGE_EVENT}

_AT_RE = re.compile(r"<@!?([^>]+)>")


class QQGroupInboundService(QQInboundService[QQGroupMessage]):
    scope = "group"

    def __init__(self, *, wake_on: str = "all", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._wake_on = wake_on

    def _parse(self, payload: dict[str, Any]) -> QQGroupMessage:
        return QQGroupMessage.from_event(payload)

    def _requester(self, message: QQGroupMessage) -> Requester:
        return Requester(
            scope="group",
            sender_id=message.member_openid,
            group_openid=message.group_openid,
        )

    def _allowed(self, message: QQGroupMessage, requester: Requester) -> bool:
        if self._policy.group_allowed(message.group_openid):
            return True
        logger.warning(
            "qq.group.blocked group_openid={} reason=not_in_allow_groups",
            message.group_openid,
        )
        return False

    def _build(self, message: QQGroupMessage, *, allow_command: bool) -> ChannelMessage:
        return build_group_channel_message(
            self._channel_name,
            message,
            allow_command=allow_command,
            suppress_direct_output=self._suppress_direct_output,
            wake_on=self._wake_on,
        )


def build_group_channel_message(
    channel_name: str,
    message: QQGroupMessage,
    *,
    allow_command: bool = False,
    suppress_direct_output: bool = False,
    wake_on: str = "all",
) -> ChannelMessage:
    session_id = f"{channel_name}:group:{message.group_openid}"
    chat_id = f"group:{message.group_openid}"
    text = strip_mention_text(message.content, message.mentions)
    was_mentioned = group_was_mentioned(message)
    context = {
        QQ_CONTEXT_KEY: exclude_none(
            {
                "scope": "group",
                "sender_id": message.member_openid,
                "sender_name": message.sender_name,
                "sender_role": message.member_role,
                "group_openid": message.group_openid,
                "message_id": message.message_id,
                "was_mentioned": was_mentioned,
            }
        )
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
                "qq.group.command_denied group_openid={} member_openid={} role={}",
                message.group_openid,
                message.member_openid,
                message.member_role,
            )

    payload = {
        "message": text,
        "message_id": message.message_id,
        "type": "text" if not message.attachments else "attachment",
        "sender_id": message.member_openid,
        "sender_name": message.sender_name,
        "sender_role": message.member_role,
        "group_openid": message.group_openid,
        "chat_type": "group",
        "was_mentioned": was_mentioned,
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
        # wake_on="mention": only @-mentions start a turn; Bub still hands
        # the model the follow-up messages that arrive while it is active.
        is_active=was_mentioned if wake_on == "mention" else True,
        context=context,
        # In tool reply mode the model replies via the qq.send tool; route
        # the direct model output to the "null" channel so it is dropped.
        # Command results (above) always stay on the direct route.
        output_channel="null" if suppress_direct_output else "",
    )


def group_was_mentioned(message: QQGroupMessage) -> bool:
    if any(mention.is_you for mention in message.mentions):
        return True
    return message.event_type == GROUP_AT_EVENT


def strip_mention_text(text: str, mentions: tuple[QQMention, ...]) -> str:
    if not text or not mentions:
        return text.strip()

    by_openid = {
        mention.member_openid: mention for mention in mentions if mention.member_openid
    }

    def _replace(match: re.Match[str]) -> str:
        mention = by_openid.get(match.group(1))
        if mention is None:
            return match.group(0)
        if mention.is_you:
            return ""
        if mention.nickname:
            return f"@{mention.nickname}"
        return match.group(0)

    cleaned = _AT_RE.sub(_replace, text)
    return re.sub(r"\s+", " ", cleaned).strip()


def resolve_group_openid(
    *, channel_name: str, session_id: str, chat_id: str
) -> str | None:
    return resolve_scoped_openid(
        "group", channel_name=channel_name, session_id=session_id, chat_id=chat_id
    )
