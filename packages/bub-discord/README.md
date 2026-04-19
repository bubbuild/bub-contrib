# bub-discord

Discord channel adapter for `bub`.

## What It Provides

- Channel implementation: `DiscordChannel` (`name = "discord"`)
- Inbound message adaptation from Discord to Bub `ChannelMessage`
- Outbound sending to Discord channels with:
  - automatic chunking at Discord's 2000-character limit
  - reply-to-latest-message behavior per session

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-discord"
```

You can also install it with Bub:

```bash
bub install bub-discord@main
```

## Configuration

`DiscordChannel` reads settings from environment variables with the `BUB_DISCORD_` prefix.

- `BUB_DISCORD_TOKEN` (required): Discord bot token
- `BUB_DISCORD_ALLOW_USERS` (optional): Comma-separated allowlist of sender identifiers
  - Supports user ID, username, and global name
- `BUB_DISCORD_ALLOW_CHANNELS` (optional): Comma-separated allowlist of channel IDs
- `BUB_DISCORD_COMMAND_PREFIX` (optional, default: `!`)
- `BUB_DISCORD_PROXY` (optional): HTTP proxy URL for Discord API

## Runtime Behavior

- Session ID format: `discord:<channel_id>`
- Inbound messages:
  - ignores bot messages
  - ignores empty text messages
  - applies allowlist filters when configured
- Message activation (`is_active = true`) when any of these is true:
  - message is in DM
  - message mentions the bot
  - content contains `bub`
  - content starts with `<prefix>bub`
  - message replies to a previous bot message
- Command detection:
  - if content starts with `<prefix>bub `, that prefix is removed first
  - if remaining content starts with `,`, message kind becomes `command`

## Payload Shape

Inbound non-command messages are encoded as JSON string content, including fields like:

- `message`
- `message_id`
- `type`
- `username`
- `full_name`
- `sender_id`
- `date`
- `channel_id`
- `guild_id`
- `reply_to_message` (when present)

## Outbound Notes

- Uses `session_id` to resolve destination channel.
- Splits long messages into multiple Discord posts.
- Replies to the latest inbound message in the same session when possible.
