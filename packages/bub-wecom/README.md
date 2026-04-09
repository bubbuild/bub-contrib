# bub-wecom

WeCom channel adapter for `bub`.

## What It Provides

- Channel implementation: `WeComChannel` (`name = "wecom"`)
- Inbound message adaptation from WeCom AI bot callbacks to Bub `ChannelMessage`
- WebSocket lifecycle delegated to the official `wecom-aibot-python-sdk`
- Markdown outbound replies through the WeCom SDK
- WeCom reply-stream integration for in-session streaming replies

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-wecom"
```

## Configuration

`WeComChannel` reads settings from environment variables with the `BUB_WECOM_` prefix.

- `BUB_WECOM_BOT_ID` (required): WeCom AI bot ID
- `BUB_WECOM_SECRET` (required): WeCom AI bot secret for long-connection mode
- `BUB_WECOM_WEBSOCKET_URL` (optional): WeCom websocket endpoint, default `wss://openws.work.weixin.qq.com`
- `BUB_WECOM_DM_POLICY` (optional): direct-message policy, one of `open`, `disabled`, or `allowlist`; default `open`
- `BUB_WECOM_ALLOW_FROM` (optional): JSON array or comma-separated allowlist for DM sender user IDs; used only when `BUB_WECOM_DM_POLICY=allowlist`
- `BUB_WECOM_GROUP_POLICY` (optional): group policy, one of `open`, `disabled`, or `allowlist`; default `open`
- `BUB_WECOM_GROUP_ALLOW_FROM` (optional): JSON array or comma-separated allowlist for group chat IDs; used only when `BUB_WECOM_GROUP_POLICY=allowlist`

## Example Environment

```bash
export BUB_WECOM_BOT_ID="your-bot-id"
export BUB_WECOM_SECRET="your-long-connection-secret"
export BUB_WECOM_WEBSOCKET_URL="wss://openws.work.weixin.qq.com"

# Optional policy controls
export BUB_WECOM_DM_POLICY="open"
export BUB_WECOM_GROUP_POLICY="open"

# Example allowlist mode
# export BUB_WECOM_DM_POLICY="allowlist"
# export BUB_WECOM_ALLOW_FROM='["alice", "bob"]'
# export BUB_WECOM_GROUP_POLICY="allowlist"
# export BUB_WECOM_GROUP_ALLOW_FROM='["wrXXX", "wrYYY"]'
```

## Runtime Behavior

- Session ID format: `wecom:<chat_id>`
- Messages starting with `,` are forwarded as Bub command messages
- Direct messages are marked active by default when allowed by policy
- Group messages delivered by the WeCom callback are marked active by default
- `group_policy` controls whether a group is allowed, not whether an allowed callback should be treated as active
- The adapter uses WeCom reply-stream capability during one inbound turn, then finishes the same reply stream with the final model output

## Payload Shape

Inbound non-command messages are encoded as JSON string content with fields such as:

- `message`
- `message_id`
- `message_type`
- `sender_id`
- `chat_type`
- `quote` (when present)

The raw WeCom callback frame is kept inside channel internals for reply-stream handling and is intentionally not exposed to the model-facing payload.

## Outbound Notes

- Standard Bub outbound messages are sent as WeCom `markdown` replies
- For replies tied to an inbound callback, the adapter uses the WeCom SDK reply-stream path and finishes the existing stream instead of creating a second standalone reply
- If no active reply-stream context exists, the adapter falls back to normal SDK `send_message()` delivery
- The adapter does not yet expose template cards or file upload helpers through Bub

## Limitations

- This package currently focuses on text/markdown interaction
- It does not yet expose template cards, file upload helpers, or rich outbound media APIs through Bub
- Bub itself currently returns a final model string, so the WeCom integration uses stream lifecycle support but does not yet emit token-by-token incremental content updates from the model

## Operational Notes

- WeCom long-connection mode allows only one active connection for the same bot at a time; starting a second instance can kick the first one offline
- The adapter assumes WeCom AI bot long-connection mode is already enabled in the WeCom admin console
- If replies appear duplicated, check whether the model was given direct access to a WeCom `response_url`; this adapter avoids exposing that callback URL in the model-visible payload

## Development

- Requires Python 3.12+
- See the root README for workspace setup instructions
