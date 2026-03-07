# bub-contrib

Contrib packages for the `bub` ecosystem.

## Packages

- `packages/bub-codex`
  - Bub plugin entry point: `codex`
  - Provides a `run_model` hook that delegates model execution to the Codex CLI.
- `packages/bub-tg-feed`
  - Bub plugin entry point: `tg-feed`
  - Provides an AMQP-based channel adapter for Telegram feed messages.
- `packages/bub-schedule`
  - Bub plugin entry point: `schedule`
  - Provides scheduling channel/tools backed by APScheduler with a JSON job store.
- `packages/bub-tapestore-sqlalchemy`
  - Bub plugin entry point: `tapestore-sqlalchemy`
  - Provides a SQLAlchemy-backed tape store for Bub conversation history.
- `packages/bub-tapestore-sqlite`
  - Bub plugin entry point: `tapestore-sqlite`
  - Provides a SQLite-backed tape store for Bub conversation history.
- `packages/bub-discord`
  - Bub plugin entry point: `discord`
  - Provides a Discord channel adapter for Bub message IO.
- `packages/bub-web-search`
  - Provides a `web.search` tool backed by the Ollama web search API.
  - Registers the tool only when `BUB_SEARCH_OLLAMA_API_KEY` is configured.

## Prerequisites

- Python 3.12+ (workspace root)
- `uv` (recommended)

## Usage

To install an individual package, run:

```bash
uv pip install git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-schedule
```

## Development Setup

Install all workspace dependencies:

```bash
uv sync
```

## License

This repository is licensed under [LICENSE](./LICENSE).
