# bub-web-search

Web search tool package for `bub`.

## What It Provides

- A Bub tool named `web.search`
- A thin wrapper around the Ollama web search HTTP API
- Plain-text formatted search results suitable for model/tool consumption

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-web-search"
```

You can also install it with Bub:

```bash
bub install bub-web-search@main
```

## Required Environment Variables

- `BUB_SEARCH_OLLAMA_API_KEY`: API key used for Ollama web search requests

## Optional Environment Variables

- `BUB_SEARCH_OLLAMA_API_BASE`: override API base URL
  - Default: `https://ollama.com/api`

## Runtime Behavior

- The package exposes a single tool: `web.search`
- The tool sends `POST <api_base>/web_search` with:
  - `query`
  - `max_results`
- Results are rendered as numbered plain text entries containing:
  - title
  - URL when available
  - content snippet when available
- If the API key is missing at import time, the tool is not registered
- When the upstream response has no usable results, the tool returns `none`

## Tool Signature

- `web.search(query: str, max_results: int = 10) -> str`

## Failure Modes

- Network/client failures return `HTTP error: ...`
- Invalid JSON responses return `error: invalid json response: ...`
- Invalid runtime configuration returns an `error: ...` string
