from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QQAttachment:
    """Attachment info in QQ C2C events."""

    content_type: str | None
    filename: str | None
    height: int | None
    width: int | None
    size: int | None
    url: str | None
    voice_wav_url: str | None
    asr_refer_text: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> QQAttachment:
        return cls(
            content_type=_optional_str(payload.get("content_type")),
            filename=_optional_str(payload.get("filename")),
            height=_optional_int(payload.get("height")),
            width=_optional_int(payload.get("width")),
            size=_optional_int(payload.get("size")),
            url=_optional_str(payload.get("url")),
            voice_wav_url=_optional_str(payload.get("voice_wav_url")),
            asr_refer_text=_optional_str(payload.get("asr_refer_text")),
        )


_MSG_ELEMENT_MAX_DEPTH = 3


@dataclass(frozen=True)
class QQMsgElement:
    """One referenced message element (quote / chat record / parallel).

    Populated when ``message_type`` is 101/102/103; nested elements carry
    merged-forward chat records. Nesting is capped to keep pathological
    payloads bounded.
    """

    content: str
    message_type: int | None
    sender_name: str | None
    elements: tuple[QQMsgElement, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, depth: int = 0) -> QQMsgElement:
        author = payload.get("author")
        if not isinstance(author, dict):
            author = {}
        nested_raw = payload.get("msg_elements")
        nested: tuple[QQMsgElement, ...] = ()
        if depth < _MSG_ELEMENT_MAX_DEPTH and isinstance(nested_raw, list):
            nested = tuple(
                cls.from_payload(item, depth=depth + 1)
                for item in nested_raw
                if isinstance(item, dict)
            )
        return cls(
            content=str(payload.get("content") or ""),
            message_type=_optional_int(payload.get("message_type")),
            sender_name=_optional_str(
                author.get("username") or author.get("nickname")
            ),
            elements=nested,
        )


def _msg_elements_from(data: dict[str, Any]) -> tuple[QQMsgElement, ...]:
    raw = data.get("msg_elements")
    if not isinstance(raw, list):
        return ()
    return tuple(
        QQMsgElement.from_payload(item) for item in raw if isinstance(item, dict)
    )


@dataclass(frozen=True)
class QQMention:
    """A single @mention entry in a QQ group event."""

    member_openid: str | None
    nickname: str | None
    is_you: bool
    scope: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> QQMention:
        return cls(
            member_openid=_optional_str(
                payload.get("member_openid")
                or payload.get("id")
                or payload.get("user_openid")
            ),
            nickname=_optional_str(payload.get("nickname") or payload.get("username")),
            is_you=bool(payload.get("is_you")),
            scope=_optional_str(payload.get("scope")),
        )


@dataclass(frozen=True)
class QQGroupMessage:
    """Normalized QQ group message event payload."""

    message_id: str
    group_openid: str
    member_openid: str
    sender_name: str | None
    content: str
    timestamp: str | None
    attachments: tuple[QQAttachment, ...]
    mentions: tuple[QQMention, ...]
    event_id: str | None
    sequence: int | None
    event_type: str | None
    member_role: str | None = None
    """Sender's role in the group: ``member`` / ``admin`` / ``owner``."""
    message_type: int | None = None
    """0=text, 3=ARK card, 101=parallel, 102=chat record, 103=quote."""
    msg_elements: tuple[QQMsgElement, ...] = ()

    @classmethod
    def from_event(cls, payload: dict[str, Any]) -> QQGroupMessage:
        data = payload.get("d")
        if not isinstance(data, dict):
            raise ValueError("qq event payload.d must be an object")

        author = data.get("author")
        if not isinstance(author, dict):
            raise ValueError("qq group event author must be an object")

        mentions_raw = data.get("mentions") or []
        if not isinstance(mentions_raw, list):
            raise ValueError("qq group event mentions must be an array")
        attachments_raw = data.get("attachments") or []
        if not isinstance(attachments_raw, list):
            raise ValueError("qq group event attachments must be an array")

        return cls(
            message_id=_required_str(data.get("id"), "id"),
            group_openid=_required_str(data.get("group_openid"), "group_openid"),
            member_openid=_required_str(
                author.get("member_openid"), "author.member_openid"
            ),
            sender_name=_optional_str(author.get("username") or author.get("nickname")),
            content=str(data.get("content") or ""),
            timestamp=_optional_str(data.get("timestamp")),
            attachments=tuple(
                QQAttachment.from_payload(item)
                for item in attachments_raw
                if isinstance(item, dict)
            ),
            mentions=tuple(
                QQMention.from_payload(item)
                for item in mentions_raw
                if isinstance(item, dict)
            ),
            event_id=_optional_str(payload.get("id")),
            sequence=_optional_int(payload.get("s")),
            event_type=_optional_str(payload.get("t")),
            member_role=_optional_str(author.get("member_role")),
            message_type=_optional_int(data.get("message_type")),
            msg_elements=_msg_elements_from(data),
        )


@dataclass(frozen=True)
class QQC2CMessage:
    """Normalized QQ C2C message event payload."""

    message_id: str
    user_openid: str
    content: str
    timestamp: str | None
    attachments: tuple[QQAttachment, ...]
    event_id: str | None
    sequence: int | None
    message_type: int | None = None
    """0=text, 3=ARK card, 101=parallel, 102=chat record, 103=quote."""
    msg_elements: tuple[QQMsgElement, ...] = ()

    @classmethod
    def from_event(cls, payload: dict[str, Any]) -> QQC2CMessage:
        data = payload.get("d")
        if not isinstance(data, dict):
            raise ValueError("qq event payload.d must be an object")

        author = data.get("author")
        if not isinstance(author, dict):
            raise ValueError("qq c2c event author must be an object")

        message_id = _required_str(data.get("id"), "id")
        user_openid = _required_str(author.get("user_openid"), "author.user_openid")
        attachments_raw = data.get("attachments") or []
        if not isinstance(attachments_raw, list):
            raise ValueError("qq c2c event attachments must be an array")

        return cls(
            message_id=message_id,
            user_openid=user_openid,
            content=str(data.get("content") or ""),
            timestamp=_optional_str(data.get("timestamp")),
            attachments=tuple(
                QQAttachment.from_payload(item)
                for item in attachments_raw
                if isinstance(item, dict)
            ),
            event_id=_optional_str(payload.get("id")),
            sequence=_optional_int(payload.get("s")),
            message_type=_optional_int(data.get("message_type")),
            msg_elements=_msg_elements_from(data),
        )


def _required_str(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"qq event field {field_name} is required")
    return text


def _optional_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
