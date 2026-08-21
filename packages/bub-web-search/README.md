# bub-web-search

Provider-selectable web tools for `bub`, covering two capabilities:

- `web.search`: search the web, backed by one search provider
- `web.read`: read a webpage as clean markdown, backed by one reader provider

Each capability has its own provider dimension, so they can be mixed freely
(e.g. SearXNG for search, Jina for reading).

## Search providers

Set `BUB_SEARCH_PROVIDER` (or `provider` in the `web-search:` config section)
to enable exactly one search provider:

- `ollama`
- `searxng`
- `jina`

If the provider is unset it is inferred from which provider-specific
configuration is present. If nothing resolves, `web.search` is not registered.

## Reader providers

There is no explicit reader selector: `web.read` is enabled by configuring a
reader-capable platform. Currently that means a `jina_api_key` (Jina Reader);
without one, `web.read` is not registered. A selector field will only be
introduced once more than one reader platform exists.

## Installation

```bash
bub install bub-web-search
```

Run `bub onboard` to select a provider and write its configuration
interactively.

## Ollama

Required:

- `BUB_SEARCH_PROVIDER=ollama`
- `BUB_SEARCH_OLLAMA_API_KEY`

Optional:

- `BUB_SEARCH_OLLAMA_API_BASE`
  - Default: `https://ollama.com/api`

The `web.search` tool accepts `query` and `max_results`.

## Jina

Required:

- `BUB_SEARCH_PROVIDER=jina` for search; `BUB_SEARCH_JINA_API_KEY` alone
  already enables the `web.read` reader
- `BUB_SEARCH_JINA_API_KEY`

Optional:

- `BUB_SEARCH_JINA_SEARCH_BASE`
  - Default: `https://s.jina.ai`
- `BUB_SEARCH_JINA_READER_BASE`
  - Default: `https://r.jina.ai`
- `BUB_SEARCH_JINA_TIMEOUT_SECONDS`
  - Default: `30`

The `web.search` tool accepts `query`. The `web.read` tool accepts `url` and
returns the page content as markdown.

All settings can also be written to the `web-search:` section of the Bub
config file, e.g.:

```yaml
web-search:
  provider: jina
  jina_api_key: jina_...
```

or mixing platforms per capability (SearXNG searches, Jina reads):

```yaml
web-search:
  provider: searxng
  searxng_base_url: https://searx.example.com
  jina_api_key: jina_...
```

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
