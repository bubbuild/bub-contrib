from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest

from bub.tools import REGISTRY
from bub_searxng_search import tools


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
    def __init__(self, *, response: FakeResponse, capture: dict[str, Any], **kwargs: Any) -> None:
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


def teardown_function() -> None:
    REGISTRY.pop(tools.TOOL_NAME, None)


def test_register_tools_skips_unconfigured_instances() -> None:
    tool_instance = tools.register_tools(lambda: tools.SearXNGSearchSettings(base_url=" "))

    assert tool_instance is None
    assert tools.TOOL_NAME not in REGISTRY


def test_register_tools_adds_tool_when_base_url_exists() -> None:
    tool_instance = tools.register_tools(lambda: tools.SearXNGSearchSettings(base_url="https://search.example.com"))

    assert tool_instance is not None
    assert REGISTRY[tools.TOOL_NAME] is tool_instance


def test_search_input_rejects_blank_query() -> None:
    with pytest.raises(ValueError, match="query must not be blank"):
        tools.SearXNGSearchInput(query="   ")


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
            response=FakeResponse(body=tools.json.dumps(payload)),
            capture=capture,
            **kwargs,
        ),
    )

    result = asyncio.run(
        tools._search(
            param=tools.SearXNGSearchInput(
                query="bub",
                max_results=1,
                categories=["general", "news"],
                engines=["google", "bing"],
                language="zh-CN",
                time_range="year",
                safe_search=2,
            ),
            settings=tools.SearXNGSearchSettings(
                base_url="https://search.example.com/",
                timeout_seconds=12,
                auth_header="X-API-Key",
                auth_value="secret",
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
    assert capture["session_kwargs"]["headers"]["User-Agent"] == tools.DEFAULT_USER_AGENT
    assert capture["session_kwargs"]["headers"]["X-API-Key"] == "secret"
    assert capture["session_kwargs"]["timeout"].total == 12
    assert "Answers:" in result
    assert "Suggestions:" in result
    assert "Infoboxes:" in result
    assert "1. Bub docs" in result
    assert "source: duckduckgo [general] 2026-04-15" in result
    assert "Extra result" not in result


def test_search_returns_http_status_message(monkeypatch) -> None:
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **kwargs: FakeSession(
            response=FakeResponse(status=403, body="Forbidden"),
            capture={},
            **kwargs,
        ),
    )

    result = asyncio.run(
        tools._search(
            param=tools.SearXNGSearchInput(query="bub"),
            settings=tools.SearXNGSearchSettings(base_url="https://search.example.com"),
        )
    )

    assert result == "HTTP 403: Forbidden"


def test_search_returns_invalid_json_error(monkeypatch) -> None:
    monkeypatch.setattr(
        aiohttp,
        "ClientSession",
        lambda **kwargs: FakeSession(
            response=FakeResponse(body="not-json"),
            capture={},
            **kwargs,
        ),
    )

    result = asyncio.run(
        tools._search(
            param=tools.SearXNGSearchInput(query="bub"),
            settings=tools.SearXNGSearchSettings(base_url="https://search.example.com"),
        )
    )

    assert result.startswith("error: invalid json response:")
