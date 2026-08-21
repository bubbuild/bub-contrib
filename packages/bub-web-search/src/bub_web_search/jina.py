from __future__ import annotations

from urllib.parse import quote

from bub_web_search.config import WebSearchSettings

WEB_USER_AGENT = "bub-web-search/1.0"
MAX_ERROR_BODY_CHARS = 500


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
        return "error: jina api key is not configured"

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
            if response.status != 200:
                snippet = body[:MAX_ERROR_BODY_CHARS]
                return f"error: jina returned status {response.status}: {snippet}"
    except TimeoutError:
        return "error: jina request timed out"
    except aiohttp.ClientError as exc:
        return f"HTTP error: {exc!s}"

    return body.strip() or "none"


async def search(*, query: str, settings: WebSearchSettings) -> str:
    base = settings.jina_search_base.strip().rstrip("/")
    if not base:
        return "error: invalid jina search base url"
    endpoint = f"{base}/?q={quote(query)}"
    # "no-content" keeps the SERP compact: titles, URLs and snippets only.
    return await _request(
        endpoint, settings=settings, extra_headers={"X-Respond-With": "no-content"}
    )


async def read(*, url: str, settings: WebSearchSettings) -> str:
    target = url.strip()
    if not target:
        return "error: url must not be blank"
    return await _request(
        reader_url(settings.jina_reader_base, target), settings=settings
    )
