from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import bub
from bub import hookimpl, tool
from bub import inquirer as bub_inquirer
from bub.tools import REGISTRY

from bub_web_search import jina, ollama, searxng
from bub_web_search.config import (
    DEFAULT_JINA_READER_BASE,
    DEFAULT_JINA_SEARCH_BASE,
    DEFAULT_OLLAMA_API_BASE,
    DEFAULT_SEARXNG_SAFE_SEARCH,
    DEFAULT_SEARXNG_TIMEOUT_SECONDS,
    DEFAULT_SEARXNG_USER_AGENT,
    WebSearchSettings,
)

if TYPE_CHECKING:
    from bub.tools import Tool

SEARCH_TOOL_NAME = "web.search"
READ_TOOL_NAME = "web.read"
CONFIG_NAME = "web-search"


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
    elif provider == "jina":
        provider_config = _onboard_jina(current)
    else:
        provider_config = _onboard_searxng(current)
    reader_config = _onboard_reader(current, provider_config)
    return {CONFIG_NAME: {"provider": provider, **provider_config, **reader_config}}


def _current_provider(current: dict[str, Any]) -> str:
    provider = current.get("provider")
    if provider in PROVIDERS:
        return str(provider)
    if current.get("ollama_api_key"):
        return "ollama"
    if current.get("searxng_base_url"):
        return "searxng"
    if current.get("jina_api_key"):
        return "jina"
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


def _onboard_jina(current: dict[str, Any]) -> dict[str, Any]:
    api_key = bub_inquirer.ask_secret("Jina API key")
    return {
        "jina_api_key": api_key or str(current.get("jina_api_key") or ""),
        "jina_search_base": bub_inquirer.ask_text(
            "Jina search base URL",
            default=str(current.get("jina_search_base") or DEFAULT_JINA_SEARCH_BASE),
        ),
        "jina_reader_base": bub_inquirer.ask_text(
            "Jina reader base URL",
            default=str(current.get("jina_reader_base") or DEFAULT_JINA_READER_BASE),
        ),
    }


def _onboard_reader(
    current: dict[str, Any], provider_config: dict[str, Any]
) -> dict[str, Any]:
    has_jina_key = bool(provider_config.get("jina_api_key") or current.get("jina_api_key"))
    enable = bub_inquirer.ask_confirm(
        "Enable the web.read tool (Jina Reader)",
        default=has_jina_key,
    )
    if not enable:
        return {}
    reader_config: dict[str, Any] = {}
    if not provider_config.get("jina_api_key"):
        api_key = bub_inquirer.ask_secret("Jina API key")
        reader_config["jina_api_key"] = api_key or str(current.get("jina_api_key") or "")
    if "jina_reader_base" not in provider_config:
        reader_config["jina_reader_base"] = bub_inquirer.ask_text(
            "Jina reader base URL",
            default=str(current.get("jina_reader_base") or DEFAULT_JINA_READER_BASE),
        )
    return reader_config


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
    """Register one platform per capability; no match means no tool."""
    REGISTRY.pop(SEARCH_TOOL_NAME, None)
    REGISTRY.pop(READ_TOOL_NAME, None)

    settings = settings_factory()
    search_tool: Tool | None = None
    if registrar := SEARCH_REGISTRARS.get(settings.resolved_provider):
        search_tool = registrar(settings)
    # Readers have no selector field: each registrar guards on its own
    # platform configuration, and the first configured one wins.
    for reader_registrar in READER_REGISTRARS.values():
        if reader_registrar(settings):
            break
    return search_tool


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


def _register_jina_search_tool(settings: WebSearchSettings) -> Tool | None:
    if not settings.jina_api_key:
        return None

    @tool(name=SEARCH_TOOL_NAME)
    async def web_search_jina(query: str) -> str:
        """Search the web with Jina Search and return SERP results."""
        return await jina.search(query=query, settings=settings)

    return web_search_jina


def _register_jina_read_tool(settings: WebSearchSettings) -> Tool | None:
    if not settings.jina_api_key:
        return None

    @tool(name=READ_TOOL_NAME)
    async def web_read_jina(url: str) -> str:
        """Read a webpage and return its main content as clean markdown."""
        return await jina.read(url=url, settings=settings)

    return web_read_jina


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


# To add a platform, implement a registrar above and list it here. The
# capability tables below are the single source of truth for both tool
# registration and onboarding choices.
SEARCH_REGISTRARS: dict[str | None, Callable[[WebSearchSettings], Tool | None]] = {
    "ollama": _register_ollama_tool,
    "searxng": _register_searxng_tool,
    "jina": _register_jina_search_tool,
}
READER_REGISTRARS: dict[str | None, Callable[[WebSearchSettings], Tool | None]] = {
    "jina": _register_jina_read_tool,
}
PROVIDERS = [name for name in SEARCH_REGISTRARS if name]


register_tools()
