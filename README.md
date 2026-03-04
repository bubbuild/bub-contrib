# bub-contrib

Contrib packages for the `bub` ecosystem.

## Packages

- `packages/bub-tg-feed`
  - Bub plugin entry point: `tg-feed`
  - Provides an AMQP-based channel adapter for Telegram feed messages.
- `packages/bub-schedule`
  - Bub plugin entry point: `schedule`
  - Provides scheduling channel/tools backed by APScheduler with a JSON job store.

## Repository Layout

```text
packages/
  bub-tg-feed/
  bub-schedule/
```

## Prerequisites

- Python 3.12+ (workspace root)
- `uv` (recommended)

## Development Setup

Install all workspace dependencies:

```bash
uv sync
```

Install contrib packages in editable mode:

```bash
uv pip install -e packages/bub-tg-feed -e packages/bub-schedule
```

## Runtime Notes

### `bub-tg-feed` environment variables

- `AMQP_URL`: RabbitMQ/AMQP connection URL
- `BUB_TELEGRAM_TOKEN`: Telegram bot token used for chat actions and bot metadata

### `bub-schedule` persistence

- Scheduled jobs are persisted to `jobs.json` under Bub runtime home.

## License

This repository is licensed under [LICENSE](./LICENSE).
