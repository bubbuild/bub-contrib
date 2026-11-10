from __future__ import annotations

from typing import TypedDict, final
from urllib.parse import quote

from bub_web_search.config import WebSearchSettings

WEB_USER_AGENT = "bub-web-search/1.0"
MAX_ERROR_BODY_CHARS = 500


@final
class JinaSearchResult(TypedDict):
    query: str
    content: str


@final
class JinaReadResult(TypedDict):
    url: str
    content: str


def render_content(result: JinaSearchResult | JinaReadResult) -> str:
    return result["content"] or "none"


def reader_url(base: str, url: str) -> str:
    base = base.strip().rstrip("/")
    target = url.strip()
    if target.startswith(f"{base}/"):
        return target
    return f"{base}/{target}"


async def _request(
    endpoint: str,
    *,
    settings: WebSearchSettings,
    extra_headers: dict[str, str] | None = None,
) -> str:
    import aiohttp

    api_key = settings.jina_api_key
    if not api_key:
        raise RuntimeError("error: jina api key is not configured")

    headers = {
        "Accept": "*/*",
        "User-Agent": WEB_USER_AGENT,
        "Authorization": f"Bearer {api_key}",
    }
    if extra_headers:
        headers.update(extra_headers)

    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=settings.resolved_jina_timeout_seconds
                )
            ) as session,
            session.get(endpoint, headers=headers) as response,
        ):
            body = await response.text()
            status = response.status
    except TimeoutError as exc:
        raise RuntimeError("error: jina request timed out") from exc
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"HTTP error: {exc!s}") from exc

    if status != 200:
        snippet = body[:MAX_ERROR_BODY_CHARS]
        raise RuntimeError(f"error: jina returned status {status}: {snippet}")
    return body.strip()


async def search(*, query: str, settings: WebSearchSettings) -> JinaSearchResult:
    base = settings.jina_search_base.strip().rstrip("/")
    if not base:
        raise RuntimeError("error: invalid jina search base url")
    endpoint = f"{base}/?q={quote(query)}"
    # "no-content" keeps the SERP compact: titles, URLs and snippets only.
    content = await _request(
        endpoint, settings=settings, extra_headers={"X-Respond-With": "no-content"}
    )
    return {"query": query, "content": content}


async def read(*, url: str, settings: WebSearchSettings) -> JinaReadResult:
    target = url.strip()
    if not target:
        raise ValueError("error: url must not be blank")
    content = await _request(
        reader_url(settings.jina_reader_base, target), settings=settings
    )
    return {"url": target, "content": content}
