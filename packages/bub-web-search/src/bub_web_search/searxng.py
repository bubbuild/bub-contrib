from __future__ import annotations

import json
import re
from collections.abc import Iterable
from json import JSONDecodeError
from typing import Any, Literal, TypedDict, final

from pydantic import BaseModel, Field, field_validator

from bub_web_search.config import WebSearchSettings

MAX_RESULTS_LIMIT = 10
MAX_SNIPPET_CHARS = 280
MAX_TITLE_CHARS = 160
_WHITESPACE_RE = re.compile(r"\s+")


class SearXNGSearchInput(BaseModel):
    query: str = Field(..., description="The search query string.")
    max_results: int = Field(
        5,
        ge=1,
        le=MAX_RESULTS_LIMIT,
        description="Maximum number of search results to return.",
    )
    categories: list[str] | None = Field(
        None,
        description="Optional list of SearXNG categories, such as general, news, or science.",
    )
    engines: list[str] | None = Field(
        None, description="Optional list of SearXNG engine names to limit the search."
    )
    language: str | None = Field(
        None, description="Optional language code, such as en-US or zh-CN."
    )
    time_range: Literal["day", "month", "year"] | None = Field(
        None, description="Optional SearXNG time filter."
    )
    safe_search: int | None = Field(
        None,
        ge=0,
        le=2,
        description="Optional safe search level: 0 off, 1 moderate, 2 strict.",
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        query = value.strip()
        if not query:
            raise ValueError("query must not be blank")
        return query


@final
class SearXNGInfobox(TypedDict):
    title: str
    url: str
    content: str


@final
class SearXNGResult(TypedDict):
    title: str
    url: str
    content: str
    engine: str
    category: str
    published_date: str


@final
class SearXNGSearchResult(TypedDict):
    answers: list[str]
    suggestions: list[str]
    infoboxes: list[SearXNGInfobox]
    results: list[SearXNGResult]


async def search(
    *, param: SearXNGSearchInput, settings: WebSearchSettings
) -> SearXNGSearchResult:
    import aiohttp

    base_url = settings.resolved_searxng_base_url
    if base_url is None:
        raise RuntimeError("error: searxng base url is not configured")

    endpoint = f"{base_url}/search"
    params = _build_request_params(param=param, settings=settings)
    headers = {
        "Accept": "application/json",
        "User-Agent": settings.resolved_searxng_user_agent,
        **settings.resolved_searxng_auth_headers,
    }

    try:
        async with (
            aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=settings.resolved_searxng_timeout_seconds
                ),
            ) as session,
            session.get(endpoint, params=params) as response,
        ):
            body = await response.text()
            status = response.status
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"HTTP error: {exc!s}") from exc
    except TimeoutError as exc:
        raise RuntimeError(
            "error: request timed out after "
            f"{settings.resolved_searxng_timeout_seconds} seconds"
        ) from exc

    if status >= 400:
        detail = _compact_text(body, limit=MAX_SNIPPET_CHARS) or "request failed"
        raise RuntimeError(f"HTTP {status}: {detail}")
    try:
        payload = json.loads(body)
    except JSONDecodeError as exc:
        raise RuntimeError(f"error: invalid json response: {exc!s}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("error: invalid json response: expected a top-level object")
    return {
        "answers": _parse_answers(payload.get("answers")),
        "suggestions": _parse_suggestions(payload.get("suggestions")),
        "infoboxes": _parse_infoboxes(payload.get("infoboxes")),
        "results": _parse_results(
            payload.get("results"), max_results=param.max_results
        ),
    }


def _build_request_params(
    *, param: SearXNGSearchInput, settings: WebSearchSettings
) -> dict[str, str | int]:
    request_params: dict[str, str | int] = {
        "q": param.query,
        "format": "json",
        "safesearch": (
            param.safe_search
            if param.safe_search is not None
            else settings.resolved_searxng_default_safe_search
        ),
    }
    if language := _clean_value(param.language) or _clean_value(
        settings.searxng_default_language
    ):
        request_params["language"] = language
    if categories := _join_csv(param.categories):
        request_params["categories"] = categories
    if engines := _join_csv(param.engines):
        request_params["engines"] = engines
    if param.time_range is not None:
        request_params["time_range"] = param.time_range
    return request_params


def render_search_result(result: SearXNGSearchResult) -> str:
    sections: list[list[str]] = []
    if result["answers"]:
        sections.append(["Answers:", *(f"- {text}" for text in result["answers"])])
    if result["suggestions"]:
        sections.append(
            ["Suggestions:", *(f"- {text}" for text in result["suggestions"])]
        )
    if result["infoboxes"]:
        lines = ["Infoboxes:"]
        for infobox in result["infoboxes"]:
            lines.append(f"- {infobox['title']}")
            if infobox["url"]:
                lines.append(f"  {infobox['url']}")
            if infobox["content"]:
                lines.append(f"  {infobox['content']}")
        sections.append(lines)
    if result["results"]:
        lines = []
        for idx, item in enumerate(result["results"], start=1):
            lines.append(f"{idx}. {item['title']}")
            if item["url"]:
                lines.append(f"   {item['url']}")
            if item["content"]:
                lines.append(f"   {item['content']}")
            metadata = [
                item["engine"],
                f"[{item['category']}]" if item["category"] else "",
                item["published_date"],
            ]
            if source := " ".join(part for part in metadata if part):
                lines.append(f"   source: {source}")
        sections.append(lines)
    return "\n\n".join("\n".join(lines) for lines in sections) or "none"


def _parse_answers(raw_answers: object) -> list[str]:
    if not isinstance(raw_answers, list):
        return []
    return [text for item in raw_answers if (text := _stringify_answer(item))]


def _stringify_answer(value: object) -> str:
    if isinstance(value, str):
        return _compact_text(value, limit=MAX_SNIPPET_CHARS)
    if isinstance(value, dict):
        text = _first_non_empty(
            value.get("answer"),
            value.get("content"),
            value.get("text"),
            value.get("title"),
        )
        return _compact_text(text, limit=MAX_SNIPPET_CHARS)
    return ""


def _parse_suggestions(raw_suggestions: object) -> list[str]:
    if not isinstance(raw_suggestions, list):
        return []
    return [
        suggestion
        for item in raw_suggestions
        if isinstance(item, str)
        and (suggestion := _compact_text(item, limit=MAX_TITLE_CHARS))
    ]


def _parse_infoboxes(raw_infoboxes: object) -> list[SearXNGInfobox]:
    if not isinstance(raw_infoboxes, list):
        return []
    infoboxes: list[SearXNGInfobox] = []
    for item in raw_infoboxes:
        if not isinstance(item, dict):
            continue
        title = _compact_text(
            _first_non_empty(
                item.get("infobox"), item.get("id"), item.get("title"), "(untitled)"
            ),
            limit=MAX_TITLE_CHARS,
        )
        content = _compact_text(
            _first_non_empty(
                item.get("content"), item.get("description"), item.get("title")
            ),
            limit=MAX_SNIPPET_CHARS,
        )
        infoboxes.append(
            {
                "title": title,
                "url": _extract_url(item),
                "content": content if content != title else "",
            }
        )
    return infoboxes


def _parse_results(raw_results: object, *, max_results: int) -> list[SearXNGResult]:
    if not isinstance(raw_results, list):
        return []

    results: list[SearXNGResult] = []
    for item in raw_results:
        if len(results) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        title = _compact_text(
            _first_non_empty(
                item.get("title"), item.get("content"), item.get("url"), "(untitled)"
            ),
            limit=MAX_TITLE_CHARS,
        )
        snippet = _compact_text(
            _first_non_empty(
                item.get("content"), item.get("snippet"), item.get("description")
            ),
            limit=MAX_SNIPPET_CHARS,
        )
        results.append(
            {
                "title": title,
                "url": _extract_url(item),
                "content": snippet if snippet != title else "",
                "engine": _clean_value(item.get("engine")),
                "category": _clean_value(item.get("category")),
                "published_date": _clean_value(item.get("publishedDate")),
            }
        )
    return results


def _extract_url(item: dict[str, Any]) -> str:
    if url := _clean_value(item.get("url")):
        return url
    raw_urls = item.get("urls")
    if not isinstance(raw_urls, list):
        return ""
    for candidate in raw_urls:
        if isinstance(candidate, str):
            if url := _clean_value(candidate):
                return url
        elif isinstance(candidate, dict):
            if url := _clean_value(candidate.get("url")):
                return url
    return ""


def _join_csv(values: Iterable[object] | None) -> str:
    if values is None:
        return ""
    parts = [part for value in values if (part := _clean_value(value))]
    return ",".join(parts)


def _clean_value(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def _compact_text(value: object, *, limit: int) -> str:
    text = _clean_value(value)
    if not text:
        return ""
    compact = _WHITESPACE_RE.sub(" ", text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _first_non_empty(*values: object) -> str:
    for value in values:
        if cleaned := _clean_value(value):
            return cleaned
    return ""
