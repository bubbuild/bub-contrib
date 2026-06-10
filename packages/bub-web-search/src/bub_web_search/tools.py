from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import bub
from bub import tool
from bub.tools import REGISTRY

from bub_web_search import ollama, searxng
from bub_web_search.config import WebSearchSettings

if TYPE_CHECKING:
    from republic import Tool

SEARCH_TOOL_NAME = "web.search"


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
