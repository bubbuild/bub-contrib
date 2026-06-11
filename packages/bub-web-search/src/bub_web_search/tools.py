from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import bub
from bub import hookimpl, tool
from bub import inquirer as bub_inquirer
from bub.tools import REGISTRY

from bub_web_search import ollama, searxng
from bub_web_search.config import (
    DEFAULT_OLLAMA_API_BASE,
    DEFAULT_SEARXNG_SAFE_SEARCH,
    DEFAULT_SEARXNG_TIMEOUT_SECONDS,
    DEFAULT_SEARXNG_USER_AGENT,
    WebSearchSettings,
)

if TYPE_CHECKING:
    from republic import Tool

SEARCH_TOOL_NAME = "web.search"
CONFIG_NAME = "web-search"
PROVIDERS = ["ollama", "searxng"]


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    existing = current_config.get(CONFIG_NAME)
    configure = bub_inquirer.ask_confirm(
        "Configure web search",
        default=isinstance(existing, dict),
    )
    if not configure:
        return None

    current = existing if isinstance(existing, dict) else {}
    provider = bub_inquirer.ask_select(
        "Web search provider",
        choices=PROVIDERS,
        default=_current_provider(current),
    )
    if provider == "ollama":
        provider_config = _onboard_ollama(current)
    else:
        provider_config = _onboard_searxng(current)
    return {CONFIG_NAME: {"provider": provider, **provider_config}}


def _current_provider(current: dict[str, Any]) -> str:
    provider = current.get("provider")
    if provider in PROVIDERS:
        return str(provider)
    if current.get("ollama_api_key"):
        return "ollama"
    if current.get("searxng_base_url"):
        return "searxng"
    return "ollama"


def _onboard_ollama(current: dict[str, Any]) -> dict[str, Any]:
    api_key = bub_inquirer.ask_secret("Ollama API key")
    return {
        "ollama_api_key": api_key or str(current.get("ollama_api_key") or ""),
        "ollama_api_base": bub_inquirer.ask_text(
            "Ollama API base URL",
            default=str(current.get("ollama_api_base") or DEFAULT_OLLAMA_API_BASE),
        ),
    }


def _onboard_searxng(current: dict[str, Any]) -> dict[str, Any]:
    base_url = bub_inquirer.ask_text(
        "SearXNG base URL",
        default=str(current.get("searxng_base_url") or ""),
    )
    timeout_seconds = int(
        bub_inquirer.ask_text(
            "SearXNG timeout seconds",
            default=str(
                current.get("searxng_timeout_seconds")
                or DEFAULT_SEARXNG_TIMEOUT_SECONDS
            ),
        )
    )
    default_language = bub_inquirer.ask_text(
        "SearXNG default language (optional)",
        default=str(current.get("searxng_default_language") or ""),
    )
    current_safe_search = current.get(
        "searxng_default_safe_search", DEFAULT_SEARXNG_SAFE_SEARCH
    )
    safe_search = bub_inquirer.ask_select(
        "SearXNG default safe search",
        choices=["0", "1", "2"],
        default=str(current_safe_search),
    )
    user_agent = bub_inquirer.ask_text(
        "SearXNG user agent",
        default=str(current.get("searxng_user_agent") or DEFAULT_SEARXNG_USER_AGENT),
    )
    auth_header = bub_inquirer.ask_text(
        "SearXNG auth header (optional)",
        default=str(current.get("searxng_auth_header") or ""),
    )
    auth_value = bub_inquirer.ask_secret("SearXNG auth value (optional)")
    return {
        "searxng_base_url": base_url,
        "searxng_timeout_seconds": timeout_seconds,
        "searxng_default_language": default_language,
        "searxng_default_safe_search": int(safe_search),
        "searxng_user_agent": user_agent,
        "searxng_auth_header": auth_header,
        "searxng_auth_value": auth_value
        or str(current.get("searxng_auth_value") or ""),
    }


def register_tools(
    settings_factory: Callable[[], WebSearchSettings] = lambda: bub.ensure_config(
        WebSearchSettings
    ),
) -> Tool | None:
    REGISTRY.pop(SEARCH_TOOL_NAME, None)

    settings = settings_factory()
    provider = settings.resolved_provider
    if provider == "ollama":
        return _register_ollama_tool(settings)
    elif provider == "searxng":
        return _register_searxng_tool(settings)
    return None


def _register_ollama_tool(settings: WebSearchSettings) -> Tool | None:
    if not settings.ollama_api_key:
        return None

    @tool(name=SEARCH_TOOL_NAME)
    async def web_search_ollama(query: str, max_results: int = 10) -> str:
        """Search the web with Ollama and return concise results."""
        return await ollama.search(
            query=query, max_results=max_results, settings=settings
        )

    return web_search_ollama


def _register_searxng_tool(settings: WebSearchSettings) -> Tool | None:
    if settings.resolved_searxng_base_url is None:
        return None

    @tool(
        name=SEARCH_TOOL_NAME,
        model=searxng.SearXNGSearchInput,
        description="Search a configured SearXNG instance and return concise web results.",
    )
    async def searxng_search(param: searxng.SearXNGSearchInput) -> str:
        return await searxng.search(param=param, settings=settings)

    return searxng_search


register_tools()
