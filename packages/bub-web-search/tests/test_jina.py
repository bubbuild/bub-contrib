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


async def test_search_requires_api_key() -> None:
    result = await jina.search(query="bub", settings=WebSearchSettings())
    assert result == "error: jina api key is not configured"


async def test_read_requires_api_key() -> None:
    result = await jina.read(
        url="https://example.com", settings=WebSearchSettings()
    )
    assert result == "error: jina api key is not configured"


async def test_read_rejects_blank_url() -> None:
    result = await jina.read(url="  ", settings=WebSearchSettings(jina_api_key="k"))
    assert result == "error: url must not be blank"


async def test_search_rejects_blank_base() -> None:
    settings = WebSearchSettings(jina_api_key="k", jina_search_base="  ")
    assert await jina.search(query="bub", settings=settings) == (
        "error: invalid jina search base url"
    )
