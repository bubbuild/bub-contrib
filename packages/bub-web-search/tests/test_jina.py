import pytest

from bub_web_search import jina
from bub_web_search.config import WebSearchSettings


def test_reader_url_prefixes_target() -> None:
    assert (
        jina.reader_url("https://r.jina.ai", "https://example.com/page")
        == "https://r.jina.ai/https://example.com/page"
    )


def test_reader_url_keeps_already_prefixed_target() -> None:
    prefixed = "https://r.jina.ai/https://example.com/page"
    assert jina.reader_url("https://r.jina.ai/", prefixed) == prefixed


def test_render_content() -> None:
    assert jina.render_content({"query": "bub", "content": "results"}) == "results"
    assert jina.render_content({"url": "https://example.com", "content": ""}) == (
        "none"
    )


async def test_search_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="^error: jina api key is not configured$"):
        await jina.search(query="bub", settings=WebSearchSettings())


async def test_read_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="^error: jina api key is not configured$"):
        await jina.read(url="https://example.com", settings=WebSearchSettings())


async def test_read_rejects_blank_url() -> None:
    with pytest.raises(ValueError, match="^error: url must not be blank$"):
        await jina.read(url="  ", settings=WebSearchSettings(jina_api_key="k"))


async def test_search_rejects_blank_base() -> None:
    settings = WebSearchSettings(jina_api_key="k", jina_search_base="  ")
    with pytest.raises(RuntimeError, match="^error: invalid jina search base url$"):
        await jina.search(query="bub", settings=settings)
