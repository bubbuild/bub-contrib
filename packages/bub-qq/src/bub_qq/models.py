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
