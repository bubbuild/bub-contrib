"""Helpers shared by C2C and group inbound adaptation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bub.channels.message import ChannelMessage
from loguru import logger

from ..guard import Requester
from ..security import QQAccessPolicy
from ..session import QQInboundDeduper
from ..session import QQSessionState
from ..session import remember_session

from ..protocol.models import QQAttachment
from ..protocol.models import QQC2CMessage
from ..protocol.models import QQGroupMessage
from ..protocol.models import QQMsgElement


def exclude_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def msg_element_payloads(
    elements: tuple[QQMsgElement, ...], *, first_index: int = 0
) -> list[dict[str, Any]] | None:
    """Referenced messages (quotes / chat records) for the model payload.

    Their attachments carry an ``index`` counted on from ``first_index``
    (the number of the message's own attachments), in the order of
    :func:`~bub_qq.protocol.models.quoted_attachments`.
    """

    payloads, _ = _msg_element_payloads(elements, first_index)
    return payloads


def _msg_element_payloads(
    elements: tuple[QQMsgElement, ...], next_index: int
) -> tuple[list[dict[str, Any]] | None, int]:
    if not elements:
        return None, next_index
    payloads: list[dict[str, Any]] = []
    for element in elements:
        attachments = attachment_payloads(element.attachments)
        if attachments is not None:
            attachments = [
                {"index": next_index + offset, **entry}
                for offset, entry in enumerate(attachments)
            ]
            next_index += len(attachments)
        nested, next_index = _msg_element_payloads(element.elements, next_index)
        payload: dict[str, Any] = {
            "message": element.content,
            "sender_name": element.sender_name,
            "attachments": attachments,
            "messages": nested,
        }
        payloads.append(exclude_none(payload))
    return payloads, next_index


def attachment_payloads(
    attachments: tuple[QQAttachment, ...],
) -> list[dict[str, Any]] | None:
    if not attachments:
        return None
    return [
        {
            "content_type": attachment.content_type,
            "filename": attachment.filename,
            "height": attachment.height,
            "width": attachment.width,
            "size": attachment.size,
            "url": attachment.url,
            "voice_wav_url": attachment.voice_wav_url,
            "asr_refer_text": attachment.asr_refer_text,
        }
        for attachment in attachments
    ]


def resolve_scoped_openid(
    scope: str, *, channel_name: str, session_id: str, chat_id: str
) -> str | None:
    """Target openid from ``chat_id`` (``<scope>:<id>``) or the session id."""

    for value, prefix in (
        (chat_id, f"{scope}:"),
        (session_id, f"{channel_name}:{scope}:"),
    ):
        if value.startswith(prefix):
            return value.removeprefix(prefix).strip() or None
    return None


class QQInboundService[M: (QQC2CMessage, QQGroupMessage)]:
    """Shared inbound pipeline: parse, dedupe, allowlist, adapt, remember.

    Subclasses supply what differs per scope: how to parse the event, who
    the sender is, the allowlist check and how the Bub message is built.
    """

    scope: str = ""

    def __init__(
        self,
        *,
        channel_name: str,
        deduper: QQInboundDeduper,
        state: QQSessionState,
        policy: QQAccessPolicy,
        suppress_direct_output: bool = False,
        is_admin: Callable[[Requester], bool] = lambda requester: False,
    ) -> None:
        self._channel_name = channel_name
        self._deduper = deduper
        self._state = state
        self._policy = policy
        self._suppress_direct_output = suppress_direct_output
        self._is_admin = is_admin

    def parse_inbound(self, payload: dict[str, Any]) -> tuple[M, ChannelMessage] | None:
        try:
            message = self._parse(payload)
        except ValueError as exc:
            logger.warning("qq.{}.invalid_payload error={}", self.scope, exc)
            return None
        if self._deduper.seen(message.message_id):
            logger.info("qq.{}.duplicate message_id={}", self.scope, message.message_id)
            return None
        requester = self._requester(message)
        if not self._allowed(message, requester):
            return None
        channel_message = self._build(
            message, allow_command=self._is_admin(requester)
        )
        remember_session(
            self._state,
            session_id=channel_message.session_id,
            message_id=message.message_id,
            timestamp=message.timestamp,
            attachments=message.inbound_attachments,
        )
        return message, channel_message

    def _parse(self, payload: dict[str, Any]) -> M:
        raise NotImplementedError

    def _requester(self, message: M) -> Requester:
        raise NotImplementedError

    def _allowed(self, message: M, requester: Requester) -> bool:
        raise NotImplementedError

    def _build(self, message: M, *, allow_command: bool) -> ChannelMessage:
        raise NotImplementedError
