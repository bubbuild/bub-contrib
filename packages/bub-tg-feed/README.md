# bub-tg-feed

AMQP-based Telegram feed channel plugin for `bub`.

This is applicable to listening for messages from the Bot.

See also: [tg-message-feed] - a companion service that forwards Telegram messages to AMQP.

## What It Provides

- Bub plugin entry point: `tg-feed`
- A channel adapter that:
  - consumes Telegram feed messages from AMQP
  - converts them to Bub `ChannelMessage`
  - emits typing actions to Telegram while Bub is processing

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-tg-feed"
```

You can also install it with Bub:

```bash
bub install bub-tg-feed@main
```

## Provide Channels

- `AMQPChannel` consuming from exchange `telegram.messages` with routing key `#`
  Messages are expected to be in the format emitted by [tg-message-feed]

## Required Environment Variables

- `AMQP_URL`: RabbitMQ/AMQP connection URL
- `BUB_TELEGRAM_TOKEN`: Telegram bot token used for bot API calls

[tg-message-feed]: https://github.com/frostming/tg-message-feed
