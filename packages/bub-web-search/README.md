# bub-web-search

Provider-selectable web search tools for `bub`.

## Providers

Set `BUB_SEARCH_PROVIDER` to enable exactly one search provider:

- `ollama` registers `web.search`
- `searxng` registers `searxng.search`

If the provider is unset or its required configuration is missing, neither tool is
registered.

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-web-search"
```

You can also install it with Bub:

```bash
bub install bub-web-search@main
```

## Ollama

Required:

- `BUB_SEARCH_PROVIDER=ollama`
- `BUB_SEARCH_OLLAMA_API_KEY`

Optional:

- `BUB_SEARCH_OLLAMA_API_BASE`
  - Default: `https://ollama.com/api`

The `web.search` tool accepts `query` and `max_results`.

## SearXNG

Required:

- `BUB_SEARCH_PROVIDER=searxng`
- `BUB_SEARCH_SEARXNG_BASE_URL`

Optional:

- `BUB_SEARCH_SEARXNG_TIMEOUT_SECONDS`
  - Default: `10`
- `BUB_SEARCH_SEARXNG_DEFAULT_LANGUAGE`
- `BUB_SEARCH_SEARXNG_DEFAULT_SAFE_SEARCH`
  - `0` off, `1` moderate, `2` strict
  - Default: `1`
- `BUB_SEARCH_SEARXNG_USER_AGENT`
  - Default: `bub-web-search/1.0`
- `BUB_SEARCH_SEARXNG_AUTH_HEADER`
- `BUB_SEARCH_SEARXNG_AUTH_VALUE`

The `searxng.search` tool accepts:

- `query`
- `max_results`
- `categories`
- `engines`
- `language`
- `time_range`
- `safe_search`

The SearXNG instance must allow JSON responses from its `/search` endpoint.

## Migration From bub-searxng-search

Replace the package with `bub-web-search`, set
`BUB_SEARCH_PROVIDER=searxng`, and rename the environment variables:

- `BUB_SEARXNG_SEARCH_BASE_URL` to `BUB_SEARCH_SEARXNG_BASE_URL`
- other `BUB_SEARXNG_SEARCH_*` variables to `BUB_SEARCH_SEARXNG_*`
