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
- `packages/bub-discord`
  - Provides a Discord channel adapter (`DiscordChannel`) for Bub message IO.
  - Note: this package currently does not expose a Bub plugin entry point.

## Prerequisites

- Python 3.12+ (workspace root)
- `uv` (recommended)

## Usage

To install invidual package, run:

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
