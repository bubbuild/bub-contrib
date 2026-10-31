import pytest

from bub_web_search import deepseek
from bub_web_search.config import WebSearchSettings


def test_parse_search_results_skips_invalid_items() -> None:
    assert deepseek._parse_search_results(
        [
            {
                "rerank_score": 1.8,
                "title": "Bub docs",
                "url": "https://example.com/docs",
                "snippet": "Short.",
                "summary": "Official documentation.",
                "content": "",
                "site_name": "Example",
                "site_icon": None,
                "date": "2026-01-01T00:00:00+00:00",
                "authority": 3,
            },
            "invalid",
            {"url": "https://example.com/untitled", "snippet": "Only snippet."},
        ]
    ) == [
        {
            "title": "Bub docs",
            "url": "https://example.com/docs",
            "summary": "Official documentation.",
            "site_name": "Example",
            "date": "2026-01-01T00:00:00+00:00",
        },
        {
            "title": "",
            "url": "https://example.com/untitled",
            "summary": "Only snippet.",
            "site_name": "",
            "date": "",
        },
    ]


def test_render_search_result() -> None:
    result: deepseek.DeepSeekSearchResult = {
        "query": "bub",
        "results": [
            {
                "title": "Bub docs",
                "url": "https://example.com/docs",
                "summary": "Official documentation.",
                "site_name": "Example",
                "date": "2026-01-01",
            },
            {"title": "", "url": "", "summary": "", "site_name": "", "date": ""},
        ],
    }

    assert deepseek.render_search_result(result) == (
        "1. Bub docs\n   https://example.com/docs\n   Example · 2026-01-01\n"
        "   Official documentation.\n2. (untitled)"
    )
    assert deepseek.render_search_result({"query": "bub", "results": []}) == "none"


def test_render_read_result() -> None:
    assert (
        deepseek.render_read_result(
            {"url": "https://example.com", "title": "Example", "content": "Body"}
        )
        == "# Example\n\nBody"
    )
    assert (
        deepseek.render_read_result(
            {"url": "https://example.com", "title": "", "content": "Body"}
        )
        == "Body"
    )
    assert (
        deepseek.render_read_result(
            {"url": "https://example.com", "title": "Example", "content": ""}
        )
        == "none"
    )


def test_error_message_prefers_openai_style_message() -> None:
    assert (
        deepseek._error_message('{"error": {"message": "Insufficient Balance"}}')
        == "Insufficient Balance"
    )
    assert deepseek._error_message("bad gateway") == "bad gateway"


async def test_search_clamps_count(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_post(path, payload, *, settings):
        captured["path"] = path
        captured["payload"] = payload
        return {"results": []}

    monkeypatch.setattr(deepseek, "_post", fake_post)
    settings = WebSearchSettings(deepseek_api_key="k")

    await deepseek.search(query="bub", max_results=100, settings=settings)
    assert captured == {
        "path": "/v1/tools/search",
        "payload": {"query": "bub", "count": 50},
    }

    await deepseek.search(query="bub", max_results=0, settings=settings)
    assert captured["payload"] == {"query": "bub", "count": 1}


async def test_read_posts_url(monkeypatch) -> None:
    async def fake_post(path, payload, *, settings):
        assert path == "/v1/tools/fetch"
        return {"title": "Example", "content": f"content of {payload['url']}"}

    monkeypatch.setattr(deepseek, "_post", fake_post)

    result = await deepseek.read(
        url=" https://example.com ", settings=WebSearchSettings(deepseek_api_key="k")
    )

    assert result == {
        "url": "https://example.com",
        "title": "Example",
        "content": "content of https://example.com",
    }


async def test_search_requires_api_key() -> None:
    with pytest.raises(
        RuntimeError, match="^error: deepseek api key is not configured$"
    ):
        await deepseek.search(query="bub", max_results=5, settings=WebSearchSettings())


async def test_read_rejects_blank_url() -> None:
    with pytest.raises(ValueError, match="^error: url must not be blank$"):
        await deepseek.read(url="  ", settings=WebSearchSettings(deepseek_api_key="k"))


async def test_search_rejects_blank_base() -> None:
    settings = WebSearchSettings(deepseek_api_key="k", deepseek_api_base="  ")
    with pytest.raises(RuntimeError, match="^error: invalid deepseek api base url$"):
        await deepseek.search(query="bub", max_results=5, settings=settings)
