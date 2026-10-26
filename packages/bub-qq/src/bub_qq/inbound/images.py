"""Image attachments the model can see.

QQ delivers attachments as URLs. Image attachments of messages addressed to
the bot are attached to the message as Bub ``MediaItem`` s, so a multimodal
model sees them directly instead of reasoning around a URL (and, lacking a
way to look at the file, trying OCR scripts). Other images stay metadata
the model can open with ``qq.view_image``.

Downloads go through the same guarded path as ``qq.fetch_attachment``:
QQ's media domains or public addresses only, a size cap, and a copy under
``inbox/`` that ``qq.send media_path`` can forward.
"""

from __future__ import annotations

import mimetypes
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from bub.channels.message import MediaItem
from loguru import logger

from ..protocol.models import QQAttachment
from ..workspace import inbox_dir
from ..workspace import safe_filename
from .persist import download_attachment

# Formats multimodal model APIs commonly accept.
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def image_mime_type(attachment: QQAttachment) -> str | None:
    """The attachment's image type, or None when it is not a usable image."""

    content_type = (attachment.content_type or "").split(";", 1)[0].strip().lower()
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    if content_type in IMAGE_TYPES:
        return content_type
    if content_type and not content_type.startswith("image/"):
        return None
    guessed, _ = mimetypes.guess_type(attachment.filename or "")
    return guessed if guessed in IMAGE_TYPES else None


async def load_image(
    attachment: QQAttachment,
    *,
    workspace: Path,
    message_id: str,
    index: int,
    max_bytes: int,
) -> bytes:
    """The image's bytes, downloaded into inbox/ once.

    Raises ``ValueError`` (including a refused address), ``OSError``,
    ``aiohttp.ClientError`` or ``TimeoutError`` with the reason it is
    unavailable.
    """

    if not attachment.url:
        raise ValueError("the attachment has no download URL")
    if attachment.size is not None and attachment.size > max_bytes:
        raise ValueError(f"the image is larger than the {max_bytes}-byte limit")
    dest = inbox_dir(workspace, message_id) / safe_filename(attachment.filename, attachment.url, index)
    if not dest.is_file():
        await download_attachment(attachment.url, dest, max_bytes=max_bytes)
    data = dest.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"the image is larger than the {max_bytes}-byte limit")
    return data


async def fetch_image(
    attachment: QQAttachment,
    *,
    workspace: Path,
    message_id: str,
    index: int,
    max_bytes: int,
) -> bytes | None:
    """The image's bytes for a ``MediaItem``; None (and a log line) when unavailable."""

    try:
        return await load_image(
            attachment, workspace=workspace, message_id=message_id, index=index, max_bytes=max_bytes
        )
    except (OSError, aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning(
            "qq.image.unavailable message_id={} index={} host={} error={}",
            message_id,
            index,
            urlparse(attachment.url or "").hostname,
            exc,
        )
        return None


def image_media(
    attachments: tuple[QQAttachment, ...],
    *,
    workspace: Path,
    message_id: str,
    max_count: int,
    max_bytes: int,
) -> tuple[list[MediaItem], list[int]]:
    """MediaItems for up to ``max_count`` image attachments, with their indices.

    Nothing is downloaded here: Bub calls each item's fetcher when it builds
    the prompt.
    """

    media: list[MediaItem] = []
    indices: list[int] = []
    for index, attachment in enumerate(attachments):
        if len(media) >= max_count:
            break
        mime_type = image_mime_type(attachment)
        if mime_type is None or not attachment.url:
            continue
        if attachment.size is not None and attachment.size > max_bytes:
            continue
        media.append(
            MediaItem(
                type="image",
                mime_type=mime_type,
                filename=attachment.filename,
                data_fetcher=partial(
                    fetch_image,
                    attachment,
                    workspace=workspace,
                    message_id=message_id,
                    index=index,
                    max_bytes=max_bytes,
                ),
            )
        )
        indices.append(index)
    return media, indices
