from bub.tools import REGISTRY

from bub_web_search import tools
from bub_web_search.config import WebSearchSettings


def test_onboard_config_collects_ollama_settings(monkeypatch) -> None:
    monkeypatch.setattr(tools.bub_inquirer, "ask_confirm", lambda *args, **kwargs: True)
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
    monkeypatch.setattr(tools.bub_inquirer, "ask_confirm", lambda *args, **kwargs: True)
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
    assert (
        tool_instance.description
        == "Search the web with Ollama and return concise results."
    )
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
