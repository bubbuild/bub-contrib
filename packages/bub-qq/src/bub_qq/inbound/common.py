"""Helpers shared by C2C and group inbound adaptation."""

from __future__ import annotations

from typing import Any

from ..protocol.models import QQAttachment
from ..protocol.models import QQMsgElement


def exclude_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def msg_element_payloads(
    elements: tuple[QQMsgElement, ...],
) -> list[dict[str, Any]] | None:
    """Referenced messages (quotes / chat records) for the model payload."""

    if not elements:
        return None
    payloads: list[dict[str, Any]] = []
    for element in elements:
        payload: dict[str, Any] = {
            "message": element.content,
            "sender_name": element.sender_name,
            "messages": msg_element_payloads(element.elements),
        }
        payloads.append(exclude_none(payload))
    return payloads


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
