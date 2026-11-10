import pytest

from bub_web_search import ollama
from bub_web_search.config import WebSearchSettings


def test_parse_search_results_skips_invalid_items() -> None:
    assert ollama._parse_search_results(
        [
            {
                "title": "Bub docs",
                "url": "https://example.com/docs",
                "content": "Official documentation.",
            },
            "invalid",
            {"url": "https://example.com/untitled"},
        ]
    ) == [
        {
            "title": "Bub docs",
            "url": "https://example.com/docs",
            "content": "Official documentation.",
        },
        {"title": "", "url": "https://example.com/untitled", "content": ""},
    ]


def test_render_search_result() -> None:
    result: ollama.OllamaSearchResult = {
        "results": [
            {
                "title": "Bub docs",
                "url": "https://example.com/docs",
                "content": "Official documentation.",
            },
            {"title": "", "url": "", "content": ""},
        ]
    }

    assert ollama.render_search_result(result) == (
        "1. Bub docs\n   https://example.com/docs\n   Official documentation.\n"
        "2. (untitled)"
    )


def test_render_search_result_returns_none_without_results() -> None:
    assert ollama.render_search_result({"results": []}) == "none"


async def test_search_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="ollama api key is not configured"):
        await ollama.search(query="bub", max_results=5, settings=WebSearchSettings())
