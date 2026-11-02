"""Outbound rich-media helpers for URL upload (msg_type=7).

Structured extras (media, plugin-owned keyboard) ride on ChannelMessage.context
under ``OUTBOUND_CONTEXT_KEY`` so ``qq.send`` can grow without a second
send tool or a parallel pipeline. Direct-mode text replies never set this.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlparse

import aiohttp
from bub.channels.message import ChannelMessage
from loguru import logger

from ..inbound.persist import download_to_path
from ..netguard import BlockedAddressError
from ..netguard import download_error_text
from ..netguard import download_allow_hosts
from ..netguard import guarded_session
from ..netguard import check_public_url
from ..protocol.errors import QQOpenAPIError
from ..workspace import MAX_INBOUND_DOWNLOAD_BYTES
from ..workspace import media_path_reason
from ..workspace import outbox_dir
from ..workspace import safe_filename

OUTBOUND_CONTEXT_KEY = "_qq_outbound"

FILE_TYPE_IMAGE = 1
FILE_TYPE_VIDEO = 2
FILE_TYPE_VOICE = 3
FILE_TYPE_FILE = 4
VALID_FILE_TYPES = frozenset(
    {FILE_TYPE_IMAGE, FILE_TYPE_VIDEO, FILE_TYPE_VOICE, FILE_TYPE_FILE}
)

_AT_USER_TAG = '<qqbot-at-user id="{}" />'
_AT_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
_VIDEO_EXT = frozenset({".mp4"})
_VOICE_EXT = frozenset({".silk", ".mp3", ".wav", ".ogg"})
_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

MediaDownloader = Callable[..., Awaitable[Path]]


@dataclass(frozen=True)
class MediaSpec:
    url: str = ""
    file_type: int = FILE_TYPE_FILE
    file_name: str | None = None
    local_path: str | None = None


def infer_file_type(url: str) -> int:
    """Guess ``file_type`` from the URL path suffix; unknown → file (4)."""

    suffix = _url_suffix(url)
    if suffix in _IMAGE_EXT:
        return FILE_TYPE_IMAGE
    if suffix in _VIDEO_EXT:
        return FILE_TYPE_VIDEO
    if suffix in _VOICE_EXT:
        return FILE_TYPE_VOICE
    return FILE_TYPE_FILE


def file_name_from_url(url: str) -> str | None:
    name = PurePosixPath(unquote(urlparse(url).path)).name.strip()
    return name or None


async def download_media_url(
    url: str,
    *,
    workspace: Path,
    file_name: str | None = None,
) -> Path:
    """Fetch ``url`` into the workspace outbox. QQ never sees the remote URL."""

    dest_dir = outbox_dir(workspace)
    name = safe_filename(file_name, url, 0)
    stem = Path(name).stem
    suffix = Path(name).suffix
    dest = dest_dir / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
    allow_hosts = download_allow_hosts()
    async with guarded_session(timeout=60, allow_hosts=allow_hosts) as session:
        await download_to_path(
            session,
            url,
            dest,
            max_bytes=MAX_INBOUND_DOWNLOAD_BYTES,
            headers=_DOWNLOAD_HEADERS,
            url_guard=lambda hop: check_public_url(hop, allow_hosts=allow_hosts),
        )
    logger.info("qq.media.downloaded url={} dest={}", url, dest)
    return dest


async def materialize_media_file(
    spec: MediaSpec,
    *,
    workspace: Path,
    download: MediaDownloader | None = None,
) -> MediaSpec:
    """Resolve a media spec to a local file, downloading ``media_url`` if needed."""

    if spec.local_path:
        return spec
    if not spec.url:
        raise ValueError("media has neither local_path nor url")
    fetcher = download or download_media_url
    try:
        dest = await fetcher(
            spec.url, workspace=workspace, file_name=spec.file_name
        )
    except (OSError, aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise QQOpenAPIError(
            status_code=0,
            trace_id=None,
            error_code=None,
            error_message=f"failed to download media_url ({spec.url}): {download_error_text(exc)}",
        ) from exc
    return MediaSpec(
        file_type=spec.file_type,
        file_name=spec.file_name or dest.name,
        local_path=str(dest),
    )


def media_dedupe_content(spec: MediaSpec) -> str:
    if spec.local_path:
        return f"media:{spec.file_type}:path:{spec.local_path}"
    return f"media:{spec.file_type}:{spec.url}"


def outbound_dedupe_content(
    content: str,
    media: MediaSpec | None,
    keyboard: dict[str, Any] | None,
) -> str:
    base = media_dedupe_content(media) if media is not None else content
    if not keyboard:
        return base
    encoded = json.dumps(keyboard, sort_keys=True, ensure_ascii=False)
    return f"{base}|keyboard:{encoded}"


def media_spec_from_args(
    media_url: str | None,
    file_type: object = None,
) -> tuple[MediaSpec | None, str | None]:
    """Parse tool/context media args.

    Returns ``(None, None)`` when no media was requested, ``(spec, None)``
    when valid, or ``(None, error)`` when the caller asked for media badly.
    """

    url = (media_url or "").strip()
    if not url:
        return None, None
    if not _is_http_url(url):
        return None, "Not sent: media_url must be an http(s) URL."
    try:
        check_public_url(url, allow_hosts=download_allow_hosts())
    except BlockedAddressError as exc:
        return None, f"Not sent: {download_error_text(exc)}"
    resolved_type = _optional_file_type(file_type)
    if resolved_type is None:
        resolved_type = infer_file_type(url)
    elif resolved_type not in VALID_FILE_TYPES:
        return None, "Not sent: file_type must be 1=image, 2=video, 3=voice, 4=file."
    return (
        MediaSpec(
            url=url, file_type=resolved_type, file_name=file_name_from_url(url)
        ),
        None,
    )


def outbound_context_for_media(spec: MediaSpec) -> dict[str, Any]:
    return build_outbound_context(media=spec)


def build_outbound_context(
    *,
    media: MediaSpec | None = None,
    keyboard: dict[str, Any] | None = None,
    at_user_ids: list[str] | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if reply_to:
        payload["reply_to"] = reply_to
    if media is not None:
        payload["file_type"] = media.file_type
        if media.url:
            payload["media_url"] = media.url
        if media.local_path:
            payload["media_path"] = media.local_path
        if media.file_name:
            payload["file_name"] = media.file_name
    if keyboard is not None:
        payload["keyboard"] = keyboard
    if at_user_ids:
        payload["at_user_ids"] = list(at_user_ids)
    if not payload:
        return {}
    return {OUTBOUND_CONTEXT_KEY: payload}


def parse_at_user_ids(value: object) -> tuple[list[str] | None, str | None]:
    if value is None or value == "":
        return None, None
    if isinstance(value, str):
        raw_items: list[object] = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        return None, "Not sent: at_user_ids must be a string or list of openids."
    ids: list[str] = []
    for item in raw_items:
        text = str(item).strip()
        if not text:
            continue
        if not _AT_USER_ID.fullmatch(text):
            return None, "Not sent: at_user_ids contains an invalid openid."
        if text not in ids:
            ids.append(text)
    return (ids or None), None


def apply_at_user_tags(content: str, user_ids: list[str] | None) -> str:
    if not user_ids:
        return content
    missing = [
        user_id
        for user_id in user_ids
        if _AT_USER_TAG.format(user_id) not in content
    ]
    if not missing:
        return content
    prefix = " ".join(_AT_USER_TAG.format(user_id) for user_id in missing)
    body = content.strip()
    return prefix if not body else f"{prefix} {body}"


def at_user_ids_from_message(message: ChannelMessage) -> list[str] | None:
    raw = message.context.get(OUTBOUND_CONTEXT_KEY)
    if not isinstance(raw, dict):
        return None
    ids, error = parse_at_user_ids(raw.get("at_user_ids"))
    if error is not None:
        logger.warning("qq.send invalid_at_user_ids error={}", error)
        return None
    return ids


def reply_to_from_message(message: ChannelMessage) -> str | None:
    """Inbound message id the reply should target (set by ``qq.send``)."""

    raw = message.context.get(OUTBOUND_CONTEXT_KEY)
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("reply_to") or "").strip()
    return value or None


def keyboard_from_message(message: ChannelMessage) -> dict[str, Any] | None:
    raw = message.context.get(OUTBOUND_CONTEXT_KEY)
    if not isinstance(raw, dict):
        return None
    keyboard = raw.get("keyboard")
    return dict(keyboard) if isinstance(keyboard, dict) else None


def keyboard_call_kwargs(keyboard: dict[str, Any] | None) -> dict[str, Any]:
    return {"keyboard": keyboard} if keyboard else {}


def media_from_message(message: ChannelMessage) -> MediaSpec | None:
    raw = message.context.get(OUTBOUND_CONTEXT_KEY)
    if not isinstance(raw, dict):
        return None
    local_path = str(raw.get("media_path") or "").strip()
    if local_path:
        file_type = _optional_file_type(raw.get("file_type"))
        if file_type is None:
            file_type = infer_file_type(local_path)
        if file_type not in VALID_FILE_TYPES:
            logger.warning("qq.send invalid_outbound_media error=bad_file_type")
            return None
        name = str(raw.get("file_name") or "").strip() or Path(local_path).name
        return MediaSpec(
            file_type=file_type, file_name=name, local_path=local_path
        )
    url = str(raw.get("media_url") or "").strip()
    if not url:
        return None
    spec, error = media_spec_from_args(url, raw.get("file_type"))
    if error is not None:
        logger.warning("qq.send invalid_outbound_media error={}", error)
        return None
    if spec is not None and spec.file_name is None:
        name = str(raw.get("file_name") or "").strip()
        if name:
            return MediaSpec(url=spec.url, file_type=spec.file_type, file_name=name)
    return spec


def resolve_media_path(
    raw: str, workspace: str | None
) -> tuple[Path | None, str | None]:
    """Resolve a local media path; only ``outbox/`` and ``inbox/`` files."""

    text = raw.strip()
    if not text:
        return None, None
    if not workspace or not str(workspace).strip():
        return None, "Not sent: media_path requires a workspace."
    root = Path(workspace).expanduser().resolve()
    path = Path(text).expanduser()
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    reason = media_path_reason(resolved, root)
    if reason is not None:
        return None, f"Not sent: {reason}"
    if not resolved.is_file():
        return None, "Not sent: media_path is not a file."
    return resolved, None


def _optional_file_type(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return -1
    return parsed


def _is_http_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _url_suffix(url: str) -> str:
    return PurePosixPath(unquote(urlparse(url).path)).suffix.lower()


def file_info_from_upload(payload: dict[str, Any]) -> str:
    file_info = str(payload.get("file_info") or "").strip()
    if not file_info:
        raise QQOpenAPIError(
            status_code=200,
            trace_id=None,
            error_code=None,
            error_message="qq file upload returned no file_info",
            response_body=payload,
        )
    return file_info
