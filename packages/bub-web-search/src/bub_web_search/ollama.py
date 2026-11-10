import json
from typing import TypedDict, final

from bub_web_search.config import WebSearchSettings

WEB_USER_AGENT = "bub-web-search/1.0"


@final
class OllamaSearchHit(TypedDict):
    title: str
    url: str
    content: str


@final
class OllamaSearchResult(TypedDict):
    results: list[OllamaSearchHit]


async def search(
    query: str, max_results: int, settings: WebSearchSettings
) -> OllamaSearchResult:
    import aiohttp

    api_key = settings.ollama_api_key
    if not api_key:
        raise RuntimeError("error: ollama api key is not configured")

    api_base = settings.ollama_api_base.rstrip("/")
    if not api_base:
        raise RuntimeError("error: invalid ollama api base url")

    endpoint = f"{api_base}/web_search"
    payload = {"query": query, "max_results": max_results}
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session,
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
            response.raise_for_status()
            data = await response.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"HTTP error: {exc!s}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"error: invalid json response: {exc!s}") from exc

    results = data.get("results") if isinstance(data, dict) else None
    return {"results": _parse_search_results(results)}


def _parse_search_results(results: object) -> list[OllamaSearchHit]:
    if not isinstance(results, list):
        return []
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "content": str(item.get("content") or ""),
        }
        for item in results
        if isinstance(item, dict)
    ]


def render_search_result(result: OllamaSearchResult) -> str:
    lines: list[str] = []
    for idx, item in enumerate(result["results"], start=1):
        lines.append(f"{idx}. {item['title'] or '(untitled)'}")
        if item["url"]:
            lines.append(f"   {item['url']}")
        if item["content"]:
            lines.append(f"   {item['content']}")
    return "\n".join(lines) if lines else "none"
