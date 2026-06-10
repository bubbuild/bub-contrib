from bub.tools import REGISTRY

from bub_web_search import tools
from bub_web_search.config import WebSearchSettings


def teardown_function() -> None:
    REGISTRY.pop(tools.SEARCH_TOOL_NAME, None)


def test_register_tools_skips_unconfigured_provider() -> None:
    tool_instance = tools.register_tools(lambda: WebSearchSettings())

    assert tool_instance is None
    assert tools.SEARCH_TOOL_NAME not in REGISTRY


def test_register_tools_skips_provider_with_missing_configuration() -> None:
    tool_instance = tools.register_tools(lambda: WebSearchSettings(provider="searxng"))

    assert tool_instance is None
    assert tools.SEARCH_TOOL_NAME not in REGISTRY


def test_register_tools_enables_ollama_tool() -> None:
    tool_instance = tools.register_tools(
        lambda: WebSearchSettings(
            provider="ollama",
            ollama_api_key="secret",
        )
    )

    assert tool_instance is not None
    assert REGISTRY[tools.SEARCH_TOOL_NAME] is tool_instance
    assert tool_instance.description == "Search the web with Ollama and return concise results."
    assert "categories" not in tool_instance.parameters["properties"]


def test_register_tools_enables_searxng_tool() -> None:
    tool_instance = tools.register_tools(
        lambda: WebSearchSettings(
            provider="searxng",
            searxng_base_url="https://search.example.com",
        )
    )

    assert tool_instance is not None
    assert REGISTRY[tools.SEARCH_TOOL_NAME] is tool_instance
    assert tool_instance.description == (
        "Search a configured SearXNG instance and return concise web results."
    )
    assert "categories" in tool_instance.parameters["properties"]


def test_register_tools_infers_provider_from_configuration() -> None:
    ollama_tool = tools.register_tools(
        lambda: WebSearchSettings(
            ollama_api_key="secret",
        )
    )

    searxng_tool = tools.register_tools(
        lambda: WebSearchSettings(
            searxng_base_url="https://search.example.com",
        )
    )

    assert ollama_tool is not None
    assert "categories" not in ollama_tool.parameters["properties"]
    assert searxng_tool is not None
    assert "categories" in searxng_tool.parameters["properties"]
    assert REGISTRY[tools.SEARCH_TOOL_NAME] is searxng_tool
