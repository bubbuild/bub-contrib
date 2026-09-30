from bub.tools import REGISTRY

from bub_web_search import jina, searxng, tools
from bub_web_search.config import WebSearchSettings


def test_onboard_config_collects_ollama_settings(monkeypatch) -> None:
    confirm_answers = iter([True, False])
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_confirm", lambda *args, **kwargs: next(confirm_answers)
    )
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_select", lambda *args, **kwargs: "ollama"
    )
    monkeypatch.setattr(
        tools.bub_inquirer,
        "ask_secret",
        lambda *args, **kwargs: "ollama-secret",
    )
    monkeypatch.setattr(
        tools.bub_inquirer,
        "ask_text",
        lambda *args, **kwargs: "https://ollama.example/api",
    )

    assert tools.onboard_config({}) == {
        "web-search": {
            "provider": "ollama",
            "ollama_api_key": "ollama-secret",
            "ollama_api_base": "https://ollama.example/api",
        }
    }


def test_onboard_config_collects_searxng_settings(monkeypatch) -> None:
    text_answers = iter(
        [
            "https://search.example.com",
            "15",
            "zh-CN",
            "bub-search/2.0",
            "X-API-Key",
        ]
    )
    select_answers = iter(["searxng", "2"])
    confirm_answers = iter([True, False])
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_confirm", lambda *args, **kwargs: next(confirm_answers)
    )
    monkeypatch.setattr(
        tools.bub_inquirer,
        "ask_select",
        lambda *args, **kwargs: next(select_answers),
    )
    monkeypatch.setattr(
        tools.bub_inquirer,
        "ask_text",
        lambda *args, **kwargs: next(text_answers),
    )
    monkeypatch.setattr(
        tools.bub_inquirer,
        "ask_secret",
        lambda *args, **kwargs: "search-secret",
    )

    assert tools.onboard_config({}) == {
        "web-search": {
            "provider": "searxng",
            "searxng_base_url": "https://search.example.com",
            "searxng_timeout_seconds": 15,
            "searxng_default_language": "zh-CN",
            "searxng_default_safe_search": 2,
            "searxng_user_agent": "bub-search/2.0",
            "searxng_auth_header": "X-API-Key",
            "searxng_auth_value": "search-secret",
        }
    }


def test_onboard_config_skips_when_declined(monkeypatch) -> None:
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_confirm", lambda *args, **kwargs: False
    )

    assert tools.onboard_config({}) is None


def test_onboard_config_preserves_existing_secrets_and_safe_search(monkeypatch) -> None:
    defaults: dict[str, str] = {}

    def ask_text(message: str, default: str = "") -> str:
        defaults[message] = default
        return default

    def ask_select(message: str, choices: list[str], default: str = "") -> str:
        defaults[message] = default
        return default

    monkeypatch.setattr(tools.bub_inquirer, "ask_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(tools.bub_inquirer, "ask_text", ask_text)
    monkeypatch.setattr(tools.bub_inquirer, "ask_select", ask_select)
    monkeypatch.setattr(tools.bub_inquirer, "ask_secret", lambda *args, **kwargs: "")

    result = tools.onboard_config(
        {
            "web-search": {
                "provider": "searxng",
                "searxng_base_url": "https://search.example.com",
                "searxng_default_safe_search": 0,
                "searxng_auth_value": "existing-secret",
            }
        }
    )

    assert defaults["Web search provider"] == "searxng"
    assert defaults["SearXNG default safe search"] == "0"
    assert result is not None
    assert result["web-search"]["searxng_auth_value"] == "existing-secret"


def test_onboard_config_collects_jina_settings(monkeypatch) -> None:
    text_answers = iter(["https://s.jina.example", "https://r.jina.example"])
    monkeypatch.setattr(tools.bub_inquirer, "ask_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_select", lambda *args, **kwargs: "jina"
    )
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_secret", lambda *args, **kwargs: "jina-secret"
    )
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_text", lambda *args, **kwargs: next(text_answers)
    )

    assert tools.onboard_config({}) == {
        "web-search": {
            "provider": "jina",
            "jina_api_key": "jina-secret",
            "jina_search_base": "https://s.jina.example",
            "jina_reader_base": "https://r.jina.example",
        }
    }


def test_onboard_config_collects_reader_with_other_provider(monkeypatch) -> None:
    text_answers = iter(["https://ollama.example/api", "https://r.jina.example"])
    secret_answers = iter(["ollama-secret", "jina-secret"])
    monkeypatch.setattr(tools.bub_inquirer, "ask_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_select", lambda *args, **kwargs: "ollama"
    )
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_secret", lambda *args, **kwargs: next(secret_answers)
    )
    monkeypatch.setattr(
        tools.bub_inquirer, "ask_text", lambda *args, **kwargs: next(text_answers)
    )

    assert tools.onboard_config({}) == {
        "web-search": {
            "provider": "ollama",
            "ollama_api_key": "ollama-secret",
            "ollama_api_base": "https://ollama.example/api",
            "jina_api_key": "jina-secret",
            "jina_reader_base": "https://r.jina.example",
        }
    }


def teardown_function() -> None:
    REGISTRY.pop(tools.SEARCH_TOOL_NAME, None)
    REGISTRY.pop(tools.READ_TOOL_NAME, None)


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
    assert (
        tool_instance.description
        == "Search the web with Ollama and return concise results."
    )
    assert "categories" not in tool_instance.parameters["properties"]
    assert tool_instance.output_schema is not None
    assert tool_instance.output_schema["title"] == "OllamaSearchResult"


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
    assert tool_instance.output_schema is not None
    assert tool_instance.output_schema["title"] == "SearXNGSearchResult"


async def test_searxng_tool_returns_structured_result_and_renders(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_search(*, param, settings) -> searxng.SearXNGSearchResult:
        captured["param"] = param
        return {
            "answers": [],
            "suggestions": [],
            "infoboxes": [],
            "results": [
                {
                    "title": "Bub docs",
                    "url": "https://example.com/docs",
                    "content": "",
                    "engine": "",
                    "category": "",
                    "published_date": "",
                }
            ],
        }

    monkeypatch.setattr(searxng, "search", fake_search)
    tool_instance = tools.register_tools(
        lambda: WebSearchSettings(searxng_base_url="https://search.example.com")
    )
    assert tool_instance is not None

    result = await tool_instance.run(query="bub", max_results=3)

    assert captured["param"] == searxng.SearXNGSearchInput(query="bub", max_results=3)
    assert result["results"][0]["url"] == "https://example.com/docs"
    assert tool_instance.render(result) == "1. Bub docs\n   https://example.com/docs"


async def test_jina_read_tool_returns_structured_result_and_renders(
    monkeypatch,
) -> None:
    async def fake_request(endpoint: str, **kwargs) -> str:
        return f"content of {endpoint}"

    monkeypatch.setattr(jina, "_request", fake_request)
    tools.register_tools(lambda: WebSearchSettings(jina_api_key="secret"))
    read_tool = REGISTRY[tools.READ_TOOL_NAME]
    assert read_tool.output_schema is not None

    result = await read_tool.run(url=" https://example.com ")

    assert result == {
        "url": "https://example.com",
        "content": "content of https://r.jina.ai/https://example.com",
    }
    assert read_tool.render(result) == result["content"]
    assert read_tool.render({"url": "https://example.com", "content": ""}) == "none"


def test_register_tools_enables_jina_tools() -> None:
    tool_instance = tools.register_tools(
        lambda: WebSearchSettings(
            provider="jina",
            jina_api_key="secret",
        )
    )

    assert tool_instance is not None
    assert REGISTRY[tools.SEARCH_TOOL_NAME] is tool_instance
    assert (
        tool_instance.description
        == "Search the web with Jina Search and return SERP results."
    )
    assert tool_instance.output_schema is not None
    assert tools.READ_TOOL_NAME in REGISTRY


def test_register_tools_registers_read_tool_alongside_other_provider() -> None:
    tool_instance = tools.register_tools(
        lambda: WebSearchSettings(
            provider="searxng",
            searxng_base_url="https://search.example.com",
            jina_api_key="secret",
        )
    )

    assert tool_instance is not None
    assert "categories" in tool_instance.parameters["properties"]
    assert tools.READ_TOOL_NAME in REGISTRY


def test_register_tools_skips_read_tool_without_jina_key() -> None:
    tools.register_tools(
        lambda: WebSearchSettings(
            provider="ollama",
            ollama_api_key="secret",
        )
    )

    assert tools.READ_TOOL_NAME not in REGISTRY


def test_register_tools_infers_jina_provider() -> None:
    tool_instance = tools.register_tools(
        lambda: WebSearchSettings(jina_api_key="secret")
    )

    assert tool_instance is not None
    assert REGISTRY[tools.SEARCH_TOOL_NAME] is tool_instance
    assert tools.READ_TOOL_NAME in REGISTRY


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
