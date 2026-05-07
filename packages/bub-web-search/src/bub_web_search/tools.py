import json

import bub
from bub import tool
from pydantic_settings import SettingsConfigDict

DEFAULT_OLLAMA_WEB_API_BASE = "https://ollama.com/api"
WEB_USER_AGENT = "bub-web-tools/1.0"


@bub.config(name="web-search")
class WebSearchSettings(bub.Settings):
    model_config = SettingsConfigDict(
        env_prefix="BUB_SEARCH_", extra="ignore", env_file=".env"
    )

    ollama_api_key: str | None = None
    ollama_api_base: str = DEFAULT_OLLAMA_WEB_API_BASE


settings = bub.ensure_config(WebSearchSettings)
if settings.ollama_api_key:

    @tool(name="web.search")
    async def web_search_ollama(query: str, max_results: int = 10) -> str:
        """Search the web for the given query and return a list of results."""
        import aiohttp

        api_key = settings.ollama_api_key
        if not api_key:
            return "error: ollama api key is not configured"

        api_base = settings.ollama_api_base
        if not api_base:
            return "error: invalid ollama api base url"

        endpoint = f"{api_base}/web_search"
        payload = {"query": query, "max_results": max_results}
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as session,
                session.post(
                    endpoint,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent": WEB_USER_AGENT,
                    },
                ) as response,
            ):
                data = await response.json()
        except aiohttp.ClientError as exc:
            return f"HTTP error: {exc!s}"
        except json.JSONDecodeError as exc:
            return f"error: invalid json response: {exc!s}"

        results = data.get("results")
        if not isinstance(results, list) or not results:
            return "none"
        return _format_search_results(results)


def _format_search_results(results: list[object]) -> str:
    lines: list[str] = []
    for idx, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "(untitled)")
        url = str(item.get("url") or "")
        content = str(item.get("content") or "")
        lines.append(f"{idx}. {title}")
        if url:
            lines.append(f"   {url}")
        if content:
            lines.append(f"   {content}")
    return "\n".join(lines) if lines else "none"
