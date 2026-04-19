# bub-searxng-search

SearXNG-backed web search tool package for `bub`.

## What It Provides

- A Bub tool named `searxng.search`
- A thin wrapper around the SearXNG JSON search API
- Plain-text formatted answers, infoboxes, and search results suitable for model/tool consumption

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-searxng-search"
```

You can also install it with Bub:

```bash
bub install bub-searxng-search@main
```

## Required Environment Variables

- `BUB_SEARXNG_SEARCH_BASE_URL`: base URL of your SearXNG instance
  - Example: `https://search.example.com`

## Optional Environment Variables

- `BUB_SEARXNG_SEARCH_TIMEOUT_SECONDS`
  - Default: `10`
- `BUB_SEARXNG_SEARCH_DEFAULT_LANGUAGE`
  - Example: `en-US`, `zh-CN`
- `BUB_SEARXNG_SEARCH_DEFAULT_SAFE_SEARCH`
  - `0` off, `1` moderate, `2` strict
  - Default: `1`
- `BUB_SEARXNG_SEARCH_USER_AGENT`
  - Default: `bub-searxng-search/1.0`
- `BUB_SEARXNG_SEARCH_AUTH_HEADER`
  - Optional custom authentication header name
- `BUB_SEARXNG_SEARCH_AUTH_VALUE`
  - Optional custom authentication header value

## Runtime Behavior

- The package exposes a single tool: `searxng.search`
- The tool sends `GET <base_url>/search` with `format=json`
- It supports these tool parameters:
  - `query`
  - `max_results`
  - `categories`
  - `engines`
  - `language`
  - `time_range`
  - `safe_search`
- Results are rendered as plain text blocks containing:
  - direct answers when available
  - infobox summaries when available
  - numbered search results with title, URL, snippet, and engine/category metadata
- If the base URL is missing at import time, the tool is not registered

## Tool Signature

- `searxng.search(query: str, max_results: int = 5, ...) -> str`

## Notes

- Your SearXNG instance must allow `format=json`
- Many public SearXNG instances disable JSON responses or rate-limit aggressively; a private instance is recommended for agent usage

## Failure Modes

- Network/client failures return `HTTP error: ...`
- Upstream HTTP failures return `HTTP <status>: ...`
- Invalid JSON responses return `error: invalid json response: ...`
