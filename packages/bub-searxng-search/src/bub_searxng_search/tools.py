from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from json import JSONDecodeError
from typing import Any, Literal

from bub import tool
from bub.tools import REGISTRY
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from republic import Tool

TOOL_NAME = "searxng.search"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_SAFE_SEARCH = 1
DEFAULT_USER_AGENT = "bub-searxng-search/1.0"
MAX_RESULTS_LIMIT = 10
MAX_SNIPPET_CHARS = 280
MAX_TITLE_CHARS = 160
_WHITESPACE_RE = re.compile(r"\s+")


class SearXNGSearchInput(BaseModel):
    query: str = Field(..., description="The search query string.")
    max_results: int = Field(5, ge=1, le=MAX_RESULTS_LIMIT, description="Maximum number of search results to return.")
    categories: list[str] | None = Field(
        None, description="Optional list of SearXNG categories, such as general, news, or science."
    )
    engines: list[str] | None = Field(None, description="Optional list of SearXNG engine names to limit the search.")
    language: str | None = Field(None, description="Optional language code, such as en-US or zh-CN.")
    time_range: Literal["day", "month", "year"] | None = Field(
        None, description="Optional SearXNG time filter."
    )
    safe_search: int | None = Field(
        None, ge=0, le=2, description="Optional safe search level: 0 off, 1 moderate, 2 strict."
    )


class SearXNGSearchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BUB_SEARXNG_SEARCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_url: str | None = None
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    default_language: str | None = None
    default_safe_search: int = DEFAULT_SAFE_SEARCH
    user_agent: str = DEFAULT_USER_AGENT
    auth_header: str | None = None
    auth_value: str | None = None

    @classmethod
    def from_env(cls) -> SearXNGSearchSettings:
        return cls()

    @property
    def resolved_base_url(self) -> str | None:
        if self.base_url is None:
            return None
        base_url = self.base_url.strip().rstrip("/")
        return base_url or None

    @property
    def is_configured(self) -> bool:
        return self.resolved_base_url is not None

    @property
    def resolved_timeout_seconds(self) -> int:
        return max(1, self.timeout_seconds)

    @property
    def resolved_default_safe_search(self) -> int:
        if self.default_safe_search in {0, 1, 2}:
            return self.default_safe_search
        return DEFAULT_SAFE_SEARCH

    @property
    def resolved_user_agent(self) -> str:
        user_agent = self.user_agent.strip()
        return user_agent or DEFAULT_USER_AGENT

    @property
    def resolved_auth_headers(self) -> dict[str, str]:
        if self.auth_header is None or self.auth_value is None:
            return {}
        header_name = self.auth_header.strip()
        header_value = self.auth_value.strip()
        if not header_name or not header_value:
            return {}
        return {header_name: header_value}


def register_tools(
    settings_factory: Callable[[], SearXNGSearchSettings] = SearXNGSearchSettings.from_env,
) -> Tool | None:
    REGISTRY.pop(TOOL_NAME, None)
    settings = settings_factory()
    if not settings.is_configured:
        return None

    @tool(
        name=TOOL_NAME,
        model=SearXNGSearchInput,
        description="Search a configured SearXNG instance and return concise web results.",
    )
    async def searxng_search(param: SearXNGSearchInput) -> str:
        return await _search(param=param, settings=settings)

    return searxng_search


async def _search(*, param: SearXNGSearchInput, settings: SearXNGSearchSettings) -> str:
    import aiohttp

    base_url = settings.resolved_base_url
    if base_url is None:
        return "error: searxng base url is not configured"

    endpoint = f"{base_url}/search"
    params = _build_request_params(param=param, settings=settings)
    headers = {
        "Accept": "application/json",
        "User-Agent": settings.resolved_user_agent,
        **settings.resolved_auth_headers,
    }

    try:
        async with (
            aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=settings.resolved_timeout_seconds),
            ) as session,
            session.get(endpoint, params=params) as response,
        ):
            body = await response.text()
            if response.status >= 400:
                detail = _compact_text(body, limit=MAX_SNIPPET_CHARS) or "request failed"
                return f"HTTP {response.status}: {detail}"
    except aiohttp.ClientError as exc:
        return f"HTTP error: {exc!s}"
    except TimeoutError:
        return f"error: request timed out after {settings.resolved_timeout_seconds} seconds"

    try:
        payload = json.loads(body)
    except JSONDecodeError as exc:
        return f"error: invalid json response: {exc!s}"
    if not isinstance(payload, dict):
        return "error: invalid json response: expected a top-level object"
    return _format_search_response(payload, max_results=param.max_results)


def _build_request_params(*, param: SearXNGSearchInput, settings: SearXNGSearchSettings) -> dict[str, str | int]:
    request_params: dict[str, str | int] = {
        "q": param.query.strip(),
        "format": "json",
        "safesearch": (
            param.safe_search if param.safe_search is not None else settings.resolved_default_safe_search
        ),
    }
    if language := _clean_value(param.language) or _clean_value(settings.default_language):
        request_params["language"] = language
    if categories := _join_csv(param.categories):
        request_params["categories"] = categories
    if engines := _join_csv(param.engines):
        request_params["engines"] = engines
    if param.time_range is not None:
        request_params["time_range"] = param.time_range
    return request_params


def _format_search_response(payload: dict[str, Any], *, max_results: int) -> str:
    lines: list[str] = []

    answer_lines = _format_answer_lines(payload.get("answers"))
    if answer_lines:
        lines.extend(["Answers:", *answer_lines])

    suggestion_lines = _format_suggestion_lines(payload.get("suggestions"))
    if suggestion_lines:
        if lines:
            lines.append("")
        lines.extend(["Suggestions:", *suggestion_lines])

    infobox_lines = _format_infobox_lines(payload.get("infoboxes"))
    if infobox_lines:
        if lines:
            lines.append("")
        lines.extend(["Infoboxes:", *infobox_lines])

    result_blocks = _format_result_blocks(payload.get("results"), max_results=max_results)
    if result_blocks:
        if lines:
            lines.append("")
        lines.extend(result_blocks)

    return "\n".join(lines) if lines else "none"


def _format_answer_lines(raw_answers: object) -> list[str]:
    if not isinstance(raw_answers, list):
        return []
    lines: list[str] = []
    for item in raw_answers:
        text = _stringify_answer(item)
        if text:
            lines.append(f"- {text}")
    return lines


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


def _format_suggestion_lines(raw_suggestions: object) -> list[str]:
    if not isinstance(raw_suggestions, list):
        return []
    lines: list[str] = []
    for item in raw_suggestions:
        if isinstance(item, str):
            suggestion = _compact_text(item, limit=MAX_TITLE_CHARS)
            if suggestion:
                lines.append(f"- {suggestion}")
    return lines


def _format_infobox_lines(raw_infoboxes: object) -> list[str]:
    if not isinstance(raw_infoboxes, list):
        return []
    lines: list[str] = []
    for item in raw_infoboxes:
        if not isinstance(item, dict):
            continue
        title = _compact_text(
            _first_non_empty(item.get("infobox"), item.get("id"), item.get("title"), "(untitled)"),
            limit=MAX_TITLE_CHARS,
        )
        content = _compact_text(
            _first_non_empty(item.get("content"), item.get("description"), item.get("title")), limit=MAX_SNIPPET_CHARS
        )
        lines.append(f"- {title}")
        if url := _extract_url(item):
            lines.append(f"  {url}")
        if content and content != title:
            lines.append(f"  {content}")
    return lines


def _format_result_blocks(raw_results: object, *, max_results: int) -> list[str]:
    if not isinstance(raw_results, list):
        return []

    lines: list[str] = []
    rendered = 0
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = _compact_text(
            _first_non_empty(item.get("title"), item.get("content"), item.get("url"), "(untitled)"),
            limit=MAX_TITLE_CHARS,
        )
        lines.append(f"{rendered + 1}. {title}")
        if url := _extract_url(item):
            lines.append(f"   {url}")
        snippet = _compact_text(
            _first_non_empty(item.get("content"), item.get("snippet"), item.get("description")),
            limit=MAX_SNIPPET_CHARS,
        )
        if snippet and snippet != title:
            lines.append(f"   {snippet}")

        metadata: list[str] = []
        if engine := _clean_value(item.get("engine")):
            metadata.append(engine)
        if category := _clean_value(item.get("category")):
            metadata.append(f"[{category}]")
        if published := _clean_value(item.get("publishedDate")):
            metadata.append(published)
        if metadata:
            lines.append(f"   source: {' '.join(metadata)}")

        rendered += 1
        if rendered >= max_results:
            break
    return lines if rendered else []


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


register_tools()
