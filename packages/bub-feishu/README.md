# bub-feishu

Feishu channel adapter for `bub`.

## What It Provides

- Channel implementation: `FeishuChannel` (`name = "feishu"`)
- Inbound message adaptation from Feishu to Bub `ChannelMessage`
- Outbound sending to Feishu chats with:
  - reply-to-latest-message behavior per session when possible
  - automatic chunking for long text outputs
- Packaged Feishu skill resources under `bub_skills/feishu`
- `feishu_send.py` supports both text and card sending via `--format text|card`
- `feishu_edit.py` updates an existing bot message

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-feishu"
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
- `chat_id`
- `chat_type`
- `message_id`
- `message_type`
- `sender_id`
- `sender_open_id`
- `sender_union_id`
- `sender_user_id`
- `tenant_key`
- `date`
- `parent_id`
- `root_id`
- `mentions`
- `is_reply_to_bot`
- `is_exact_bot_mentioned`
- `event_type`

## Outbound Notes

- Uses `session_id` to resolve destination chat.
- Prefers replying to the latest inbound message in the same session when possible.
- Splits long text output into multiple Feishu messages.
- Reaction support is available through the Feishu message reaction API.
