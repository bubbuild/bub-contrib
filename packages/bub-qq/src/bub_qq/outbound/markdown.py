"""Detect QQ-supported markdown and fall back to plain text when needed."""

from __future__ import annotations

import re
from typing import Protocol

from loguru import logger

from ..protocol.errors import QQOpenAPIError

_MARKDOWN_PATTERN = re.compile(
    r"("
    r"\*\*[^*\n]+\*\*"
    r"|__[^_\n]+__"
    r"|~~[^~\n]+~~"
    r"|(?<!\*)\*[^*\n]+\*(?!\*)"
    r"|(?<!_)_[^_\n]+_(?!_)"
    r"|^\s{0,3}#{1,6}\s+\S"
    r"|^\s{0,3}[-*+]\s+\S"
    r"|^\s{0,3}\d+\.\s+\S"
    r"|^\s{0,3}>\s+\S"
    r"|\[[^\]]+\]\([^)\s]+\)"
    r")",
    re.MULTILINE,
)

MARKDOWN_FALLBACK_CODES = {
    304036,
    50041,
    50042,
    50054,
    50055,
    50056,
    50057,
}


class MarkdownSender(Protocol):
    async def __call__(
        self,
        *,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]: ...


def looks_like_markdown(content: str) -> bool:
    return bool(_MARKDOWN_PATTERN.search(content))


def is_markdown_fallback_error(exc: QQOpenAPIError) -> bool:
    return exc.error_code in MARKDOWN_FALLBACK_CODES


async def send_with_markdown_fallback(
    *,
    content: str,
    msg_id: str,
    msg_seq: int,
    send_text: MarkdownSender,
    send_markdown: MarkdownSender | None,
    force_markdown: bool = False,
) -> dict[str, object]:
    use_markdown = send_markdown is not None and (
        force_markdown or looks_like_markdown(content)
    )
    if use_markdown:
        try:
            return await send_markdown(
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
        except QQOpenAPIError as exc:
            if force_markdown or not is_markdown_fallback_error(exc):
                raise
            logger.warning(
                "qq.send markdown_fallback code={} msg={}",
                exc.error_code,
                exc.error_message,
            )
    return await send_text(content=content, msg_id=msg_id, msg_seq=msg_seq)
