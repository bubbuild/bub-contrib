# bub-feishu

Feishu channel adapter for `bub`.

## What It Provides

- Channel implementation: `FeishuChannel` (`name = "feishu"`)
- Inbound message adaptation from Feishu to Bub `ChannelMessage`
- Packaged Feishu skill resources under `skills/feishu`
- `feishu_send.py` supports both text and card sending via `--format text|card`
- `feishu_edit.py` updates an existing bot message

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-feishu"
```

You can also install it with Bub:

```bash
bub install bub-feishu@main
```

## Configuration

`FeishuChannel` reads settings from environment variables with the `BUB_FEISHU_` prefix.

- `BUB_FEISHU_APP_ID` (required): Feishu app ID
- `BUB_FEISHU_APP_SECRET` (required): Feishu app secret
- `BUB_FEISHU_VERIFICATION_TOKEN` (optional): webhook verification token
- `BUB_FEISHU_ENCRYPT_KEY` (optional): webhook encrypt key
- `BUB_FEISHU_ALLOW_USERS` (optional): JSON array or comma-separated allowlist of sender user identifiers
- `BUB_FEISHU_ALLOW_CHATS` (optional): JSON array or comma-separated allowlist of chat IDs
- `BUB_FEISHU_BOT_OPEN_ID` (optional): implementation-specific bot open ID used for exact mention matching in group chats; this is not the Feishu app ID
- `BUB_FEISHU_LOG_LEVEL` (optional, default: `INFO`)

## Runtime Behavior

- Session ID format: `feishu:<chat_id>`
- Inbound messages:
  - ignores messages missing `chat_id` or `message_id`
  - applies allowlist filters when configured
  - treats messages starting with `,` as Bub commands
- Message activation (`is_active = true`) when any of these is true:
  - message is from `p2p`
  - content contains `bub`
  - content starts with `,`
  - message mentions the bot
  - message replies to a previous bot message

## Payload Shape

Inbound non-command messages are encoded as JSON string content, including fields like:

- `message`
- `message_id`
- `type`
- `sender_id`
- `sender_name`
- `sender_is_bot`
- `date`
- `media`
- `reply_to_message`

## Outbound Notes

- Inbound non-command messages set `output_channel="null"` to disable channel outbound routing.
- The channel is inbound-only; Feishu send/edit/reaction actions are handled through the packaged scripts or direct OpenAPI calls.
- Reaction support is available through the Feishu message reaction API.

## TODO

- Evaluate Feishu bot menu / chat menu tree as a Telegram-like command entrypoint for common actions and submenus.
- Evaluate a Feishu-native processing feedback mechanism comparable to Telegram typing updates.
- Evaluate whether voice-message handling is feasible and worth supporting in the Feishu skill/runtime.
