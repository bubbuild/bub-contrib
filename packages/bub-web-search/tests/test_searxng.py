from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest

from bub_web_search import searxng
from bub_web_search.config import DEFAULT_SEARXNG_USER_AGENT, WebSearchSettings


class FakeResponse:
    def __init__(self, *, status: int = 200, body: str = "{}") -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def text(self) -> str:
        return self._body


class FakeSession:
    def __init__(
        self, *, response: FakeResponse, capture: dict[str, Any], **kwargs: Any
    ) -> None:
        self._response = response
        self._capture = capture
        self._capture["session_kwargs"] = kwargs

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    def get(self, url: str, *, params: dict[str, Any]) -> FakeResponse:
        self._capture["url"] = url
        self._capture["params"] = params
        return self._response


def test_search_input_rejects_blank_query() -> None:
    with pytest.raises(ValueError, match="query must not be blank"):
        searxng.SearXNGSearchInput(query="   ")


def test_search_formats_answers_infoboxes_and_results(monkeypatch) -> None:
    capture: dict[str, Any] = {}
    payload = {
        "answers": ["Bub is a hook-first AI framework."],
        "suggestions": ["bub framework"],
        "infoboxes": [
            {
                "infobox": "Bub",
                "content": "A hook-first AI framework.",
                "urls": [{"url": "https://example.com/bub"}],
            }
        ],
        "results": [
            {
                "title": "Bub docs",
                "url": "https://example.com/docs",
                "content": "Official documentation for Bub.",
                "engine": "duckduckgo",
                "category": "general",
                "publishedDate": "2026-04-15",
            },
            {
                "title": "Extra result",
                "url": "https://example.com/extra",
                "content": "Should be truncated by max_results.",
            },
        ],
    }

    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **kwargs: FakeSession(
            response=FakeResponse(body=searxng.json.dumps(payload)),
            capture=capture,
            **kwargs,
        ),
    )

    result = asyncio.run(
        searxng.search(
            param=searxng.SearXNGSearchInput(
                query="bub",
                max_results=1,
                categories=["general", "news"],
                engines=["google", "bing"],
                language="zh-CN",
                time_range="year",
                safe_search=2,
            ),
            settings=WebSearchSettings(
                searxng_base_url="https://search.example.com/",
                searxng_timeout_seconds=12,
                searxng_auth_header="X-API-Key",
                searxng_auth_value="secret",
            ),
        )
    )

    assert capture["url"] == "https://search.example.com/search"
    assert capture["params"] == {
        "q": "bub",
        "format": "json",
        "safesearch": 2,
        "language": "zh-CN",
        "categories": "general,news",
        "engines": "google,bing",
        "time_range": "year",
    }
    assert capture["session_kwargs"]["headers"]["Accept"] == "application/json"
    assert (
        capture["session_kwargs"]["headers"]["User-Agent"] == DEFAULT_SEARXNG_USER_AGENT
    )
    assert capture["session_kwargs"]["headers"]["X-API-Key"] == "secret"
    assert capture["session_kwargs"]["timeout"].total == 12
    assert result == {
        "answers": ["Bub is a hook-first AI framework."],
        "suggestions": ["bub framework"],
        "infoboxes": [
            {
                "title": "Bub",
                "url": "https://example.com/bub",
                "content": "A hook-first AI framework.",
            }
        ],
        "results": [
            {
                "title": "Bub docs",
                "url": "https://example.com/docs",
                "content": "Official documentation for Bub.",
                "engine": "duckduckgo",
                "category": "general",
                "published_date": "2026-04-15",
            }
        ],
    }
    assert searxng.render_search_result(result) == (
        "Answers:\n"
        "- Bub is a hook-first AI framework.\n"
        "\n"
        "Suggestions:\n"
        "- bub framework\n"
        "\n"
        "Infoboxes:\n"
        "- Bub\n"
        "  https://example.com/bub\n"
        "  A hook-first AI framework.\n"
        "\n"
        "1. Bub docs\n"
        "   https://example.com/docs\n"
        "   Official documentation for Bub.\n"
        "   source: duckduckgo [general] 2026-04-15"
    )


def test_search_parses_results_and_dedupes_title_snippet(monkeypatch) -> None:
    payload = {
        "results": [
            "invalid",
            {"content": "Only   a\nsnippet", "engine": "bing"},
            {"title": "Second", "urls": ["https://example.com/second"]},
        ]
    }
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **kwargs: FakeSession(
            response=FakeResponse(body=searxng.json.dumps(payload)),
            capture={},
            **kwargs,
        ),
    )

    result = asyncio.run(
        searxng.search(
            param=searxng.SearXNGSearchInput(query="bub"),
            settings=WebSearchSettings(searxng_base_url="https://search.example.com"),
        )
    )

    assert result["answers"] == result["suggestions"] == result["infoboxes"] == []
    assert result["results"] == [
        {
            "title": "Only a snippet",
            "url": "",
            "content": "",
            "engine": "bing",
            "category": "",
            "published_date": "",
        },
        {
            "title": "Second",
            "url": "https://example.com/second",
            "content": "",
            "engine": "",
            "category": "",
            "published_date": "",
        },
    ]
    assert searxng.render_search_result(result) == (
        "1. Only a snippet\n   source: bing\n2. Second\n   https://example.com/second"
    )


def test_search_returns_empty_result_for_empty_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **kwargs: FakeSession(
            response=FakeResponse(body="{}"), capture={}, **kwargs
        ),
    )

    result = asyncio.run(
        searxng.search(
            param=searxng.SearXNGSearchInput(query="bub"),
            settings=WebSearchSettings(searxng_base_url="https://search.example.com"),
        )
    )

    assert result == {"answers": [], "suggestions": [], "infoboxes": [], "results": []}
    assert searxng.render_search_result(result) == "none"


def test_search_raises_http_status_message(monkeypatch) -> None:
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **kwargs: FakeSession(
            response=FakeResponse(status=403, body="Forbidden"),
            capture={},
            **kwargs,
        ),
    )

    with pytest.raises(RuntimeError, match="^HTTP 403: Forbidden$"):
        asyncio.run(
            searxng.search(
                param=searxng.SearXNGSearchInput(query="bub"),
                settings=WebSearchSettings(
                    searxng_base_url="https://search.example.com"
                ),
            )
        )


def test_search_raises_invalid_json_error(monkeypatch) -> None:
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **kwargs: FakeSession(
            response=FakeResponse(body="not-json"),
            capture={},
            **kwargs,
        ),
    )

    with pytest.raises(RuntimeError, match="^error: invalid json response:"):
        asyncio.run(
            searxng.search(
                param=searxng.SearXNGSearchInput(query="bub"),
                settings=WebSearchSettings(
                    searxng_base_url="https://search.example.com"
                ),
            )
        )
