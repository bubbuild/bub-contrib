# bub-qq

QQ Open Platform channel adapter for `bub`.

## Usage

Install from the monorepo package directory during local development:

```bash
uv add --editable /path/to/bub-contrib/packages/bub-qq
```

Install directly from GitHub:

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-qq"
```

You can also install it with Bub:

```bash
bub install bub-qq@main
```

## Official Documentation

For QQ bot setup, application creation, credential management, and API details, see the official QQ Bot documentation:

- [QQ Bot Developer Documentation](https://bot.q.qq.com/wiki/develop/api-v2/)

You will typically need the official docs to locate or configure:

- `APPID`
- `SECRET`
- Event subscription and callback settings
- WebSocket and OpenAPI behavior

QQ currently treats Webhook and WebSocket callbacks as mutually exclusive receive modes. According to the QQ bot configuration page, after a valid HTTPS callback URL is configured successfully, WebSocket callback delivery is no longer supported. This plugin therefore requires an explicit `BUB_QQ_RECEIVE_MODE` so runtime behavior matches the platform-side configuration.

## Status

This package currently provides a working QQ Open Platform channel integration for Bub, focused on C2C message receive and reply flows.

Current coverage:

- QQ Open Platform config loading via `BUB_QQ_*` environment variables
- Access token acquisition from `https://bots.qq.com/app/getAppAccessToken`
- Cached token refresh with the official `60` second renewal window
- A reusable `aiohttp`-based OpenAPI client that injects `Authorization: QQBot {ACCESS_TOKEN}`
- Embedded `http-webhook` receiver with callback validation (`op = 13`)
- QQ callback validation signature generation using the documented ed25519 seed derivation flow
- Webhook request signature verification using `X-Signature-Ed25519` and `X-Signature-Timestamp`
- Receive transport switch: `webhook` or `websocket`
- `C2C_MESSAGE_CREATE` parsing and Bub `ChannelMessage` adaptation for single-chat inbound events
- Inbound `msg_id` dedupe cache to avoid duplicate passive replies on repeated deliveries
- C2C text replies through `POST /v2/users/{openid}/messages` using passive reply `msg_id + msg_seq`
- Standard Bub outbound routing for normal QQ text replies
- In-memory idempotency for repeated sends on the same `session_id + msg_id`
- OpenAPI failures now expose HTTP status, platform business code, and `X-Tps-trace-ID`
- OpenAPI known error codes now live in a dedicated catalog module with category and retryability metadata
- WebSocket close codes now distinguish fatal stop conditions from reconnectable conditions
- WebSocket shard orchestration using `/gateway/bot` recommended shard count
- Per-shard websocket session state for reconnect and resume
- Identify pacing based on `session_start_limit.max_concurrency`
- Automated test coverage for config, auth, signatures, channel behavior, webhook flow, websocket flow, gateway handling, and C2C services

Current limitations:

- QQ group / channel / DM send APIs
- Broader webhook event coverage beyond validation and the current basic `{"op":12}` acknowledgement flow
- Group and other QQ event types
- Dynamic shard rebalancing or in-process shard-count refresh after startup

## Confirmed Interface Rules

Based on the official QQ Bot docs for "API Calls and Authentication":

- Token endpoint: `POST https://bots.qq.com/app/getAppAccessToken`
- Request body fields: `appId`, `clientSecret`
- Token lifetime: up to `7200` seconds
- Renewal rule: when the current token is within `60` seconds of expiry, requesting again returns a new token while the old token remains valid during that `60` second overlap
- OpenAPI base URL: `https://api.sgroup.qq.com`
- Required auth header for OpenAPI requests: `Authorization: QQBot {ACCESS_TOKEN}`
- OpenAPI trace header: `X-Tps-trace-ID`

Based on the official QQ Bot docs for "Event Subscription and Notifications":

- Webhook callbacks must use HTTPS in production
- Allowed callback ports are `80`, `443`, `8080`, `8443`
- After a valid HTTPS callback URL is configured successfully, WebSocket callback delivery is no longer supported
- Validation requests arrive with `op = 13`
- Validation response must include `plain_token` and an ed25519 signature over `event_ts + plain_token`
- Normal webhook requests are verified against `timestamp + raw_body`
- Normal event pushes use the shared payload shape `{id, op, d, s, t}`
- `C2C_MESSAGE_CREATE` belongs to `GROUP_AND_C2C_EVENT (1 << 25)`
- `C2C_MESSAGE_CREATE.d` currently maps these documented fields: `id`, `author.user_openid`, `content`, `timestamp`, `attachments`
- Bub session ID format for C2C is `qq:c2c:<user_openid>`
- Bub chat ID format for C2C is `c2c:<user_openid>`
- C2C outbound currently sends text with `msg_type = 0`
- C2C outbound uses passive reply only; active push is intentionally not used because the official doc states it stopped being provided on April 21, 2025
- `websocket` mode currently uses `GROUP_AND_C2C_EVENT (1 << 25)` by default
- WebSocket close codes `4914` and `4915` are treated as fatal stop conditions
- WebSocket close codes such as `4006`, `4007`, `4008`, `4009`, and `4900~4913` are treated as reconnectable

## Environment Variables

Required:

- `BUB_QQ_APPID`: QQ bot app ID
- `BUB_QQ_SECRET`: QQ bot secret
- `BUB_QQ_RECEIVE_MODE`: inbound transport mode, must be `webhook` or `websocket`

`BUB_QQ_RECEIVE_MODE` controls which receive transport the plugin starts:

- `webhook`: starts the embedded webhook server only; WebSocket is not started
- `websocket`: starts the WebSocket client only; the embedded webhook server is not started

Optional:

- `BUB_QQ_TOKEN_URL`: override token endpoint if needed; defaults to `https://bots.qq.com/app/getAppAccessToken`
- `BUB_QQ_OPENAPI_BASE_URL`: override OpenAPI base URL if needed; defaults to `https://api.sgroup.qq.com`
- `BUB_QQ_TIMEOUT_SECONDS`: HTTP timeout for token and OpenAPI requests; defaults to `30`
- `BUB_QQ_TOKEN_REFRESH_SKEW_SECONDS`: token refresh lead time; defaults to `60`
- `BUB_QQ_WEBHOOK_HOST`: embedded webhook bind host; defaults to `127.0.0.1`
- `BUB_QQ_WEBHOOK_PORT`: embedded webhook bind port; defaults to `8080`. QQ currently allows callback ports `80`, `443`, `8080`, and `8443`
- `BUB_QQ_WEBHOOK_PATH`: webhook path; defaults to `/qq/webhook`
- `BUB_QQ_WEBHOOK_CALLBACK_TIMEOUT_SECONDS`: reserved for future callback handling controls; defaults to `15`
- `BUB_QQ_VERIFY_SIGNATURE`: whether to enforce webhook request signature validation; defaults to `true`
- `BUB_QQ_INBOUND_DEDUPE_SIZE`: recent `msg_id` cache size; defaults to `1024`
- `BUB_QQ_WEBSOCKET_INTENTS`: websocket identify intents; defaults to `1 << 25`
- `BUB_QQ_WEBSOCKET_USE_SHARD_GATEWAY`: whether to call `/gateway/bot` and start the recommended number of shard connections; defaults to `false`
- `BUB_QQ_WEBSOCKET_RECONNECT_DELAY_SECONDS`: reconnect delay after websocket disconnect; defaults to `5`
