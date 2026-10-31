from __future__ import annotations

import json
from typing import Any, TypedDict, final

from bub_web_search.config import WebSearchSettings

WEB_USER_AGENT = "bub-web-search/1.0"
MAX_ERROR_BODY_CHARS = 500
MIN_SEARCH_COUNT = 1
MAX_SEARCH_COUNT = 50


@final
class DeepSeekSearchHit(TypedDict):
    title: str
    url: str
    summary: str
    site_name: str
    date: str


@final
class DeepSeekSearchResult(TypedDict):
    query: str
    results: list[DeepSeekSearchHit]


@final
class DeepSeekReadResult(TypedDict):
    url: str
    title: str
    content: str


async def _post(
    path: str, payload: dict[str, Any], *, settings: WebSearchSettings
) -> dict[str, Any]:
    import aiohttp

    api_key = settings.deepseek_api_key
    if not api_key:
        raise RuntimeError("error: deepseek api key is not configured")

    api_base = settings.deepseek_api_base.strip().rstrip("/")
    if not api_base:
        raise RuntimeError("error: invalid deepseek api base url")

    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=settings.resolved_deepseek_timeout_seconds
                )
            ) as session,
            session.post(
                f"{api_base}{path}",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": WEB_USER_AGENT,
                },
            ) as response,
        ):
            body = await response.text()
            status = response.status
    except TimeoutError as exc:
        raise RuntimeError("error: deepseek request timed out") from exc
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"HTTP error: {exc!s}") from exc

    if status != 200:
        raise RuntimeError(
            f"error: deepseek returned status {status}: {_error_message(body)}"
        )
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"error: invalid json response: {exc!s}") from exc
    return data if isinstance(data, dict) else {}


def _error_message(body: str) -> str:
    # DeepSeek uses the OpenAI-compatible {"error": {"message": ...}} shape.
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return body[:MAX_ERROR_BODY_CHARS]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return body[:MAX_ERROR_BODY_CHARS]


async def search(
    *, query: str, max_results: int, settings: WebSearchSettings
) -> DeepSeekSearchResult:
    count = min(max(max_results, MIN_SEARCH_COUNT), MAX_SEARCH_COUNT)
    data = await _post(
        "/v1/tools/search", {"query": query, "count": count}, settings=settings
    )
    return {"query": query, "results": _parse_search_results(data.get("results"))}


def _parse_search_results(results: object) -> list[DeepSeekSearchHit]:
    if not isinstance(results, list):
        return []
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            # "summary" is the longer, model-oriented abstract; fall back to
            # the short UI snippet when it is missing.
            "summary": str(item.get("summary") or item.get("snippet") or ""),
            "site_name": str(item.get("site_name") or ""),
            "date": str(item.get("date") or ""),
        }
        for item in results
        if isinstance(item, dict)
    ]


async def read(*, url: str, settings: WebSearchSettings) -> DeepSeekReadResult:
    target = url.strip()
    if not target:
        raise ValueError("error: url must not be blank")
    data = await _post("/v1/tools/fetch", {"url": target}, settings=settings)
    return {
        "url": target,
        "title": str(data.get("title") or ""),
        "content": str(data.get("content") or ""),
    }


def render_search_result(result: DeepSeekSearchResult) -> str:
    lines: list[str] = []
    for idx, item in enumerate(result["results"], start=1):
        lines.append(f"{idx}. {item['title'] or '(untitled)'}")
        if item["url"]:
            lines.append(f"   {item['url']}")
        meta = " · ".join(part for part in (item["site_name"], item["date"]) if part)
        if meta:
            lines.append(f"   {meta}")
        if item["summary"]:
            lines.append(f"   {item['summary']}")
    return "\n".join(lines) if lines else "none"


def render_read_result(result: DeepSeekReadResult) -> str:
    if not result["content"]:
        return "none"
    if result["title"]:
        return f"# {result['title']}\n\n{result['content']}"
    return result["content"]
