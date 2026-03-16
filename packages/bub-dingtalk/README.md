# bub-dingtalk

DingTalk channel adapter for `bub`.

## What It Provides

- Channel implementation: `DingTalkChannel` (`name = "dingtalk"`)
- Inbound message adaptation from DingTalk Stream Mode to Bub `ChannelMessage`
- Outbound sending through the packaged DingTalk script helper
- Supports private (1:1) and group chats with group mapping via `group:<openConversationId>`

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-dingtalk"
```

## Configuration

`DingTalkChannel` reads settings from environment variables with the `BUB_DINGTALK_` prefix.

- `BUB_DINGTALK_CLIENT_ID` (required): AppKey from DingTalk Open Platform
- `BUB_DINGTALK_CLIENT_SECRET` (required): AppSecret
- `BUB_DINGTALK_ALLOW_USERS` (optional): Comma-separated allowlist of sender staff IDs, or `*` for all

## Runtime Behavior

- Session ID format: `dingtalk:<chat_id>`
- Inbound messages:
  - ignores senders outside `BUB_DINGTALK_ALLOW_USERS`
  - ignores empty or unsupported inbound message bodies
  - maps group conversations to `group:<openConversationId>`
- Message activation (`is_active = true`) is always enabled for allowed inbound messages
- Command detection:
  - if content starts with `,`, message kind becomes `command`

## Payload Shape

Inbound messages keep the original DingTalk text as plain string `content`.

- `session_id`: `dingtalk:<chat_id>`
- `channel`: `dingtalk`
- `chat_id`: sender staff ID for 1:1, or `group:<openConversationId>` for groups
- `kind`: `command` when content starts with `,`, otherwise `normal`
- `is_active`: always `true` for allowed inbound messages

## Outbound Notes

- Uses `chat_id` directly when present, otherwise falls back to the `session_id` suffix.
- Delegates outbound delivery to `skills.dingtalk.scripts.dingtalk_send.send_message()`.
- Uses standard TLS verification; there is no global SSL monkey patching or per-request verification bypass.

## Verify Inbound Flow

The packaged skill resources live under `src/skills/dingtalk`.

To simulate the inbound path (DingTalk -> agent loop):

```bash
# From workspace root
uv run --isolated --no-project --with-editable ./packages/bub-dingtalk python packages/bub-dingtalk/tests/test_inbound_flow.py
```

Or run the pytest:

```bash
uv run --isolated --no-project --with-editable ./packages/bub-dingtalk --with pytest python -m pytest packages/bub-dingtalk/tests/test_inbound_flow.py -v
```
