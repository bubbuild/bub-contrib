# bub-qq

QQ Open Platform channel adapter for `bub`.

## Status

This package is under active integration.

Implemented today:

- QQ Open Platform config loading via `BUB_QQ_*` environment variables
- Access token acquisition from `https://bots.qq.com/app/getAppAccessToken`
- Cached token refresh with the official `60` second renewal window
- A reusable `httpx`-based OpenAPI client that injects `Authorization: QQBot {ACCESS_TOKEN}`
- Embedded `http-webhook` receiver with callback validation (`op = 13`)
- QQ callback validation signature generation using the documented ed25519 seed derivation flow
- Webhook request signature verification using `X-Signature-Ed25519` and `X-Signature-Timestamp`
- Receive transport switch: `webhook` or `websocket`
- `C2C_MESSAGE_CREATE` parsing and Bub `ChannelMessage` adaptation for single-chat inbound events
- Inbound `msg_id` dedupe cache to avoid duplicate passive replies on repeated deliveries
- C2C text replies through `POST /v2/users/{openid}/messages` using passive reply `msg_id + msg_seq`
- OpenAPI failures now expose HTTP status, platform business code, and `X-Tps-trace-ID`
- OpenAPI known error codes now live in a dedicated catalog module with category and retryability metadata
- WebSocket close codes now distinguish fatal stop conditions from reconnectable conditions

Not implemented yet:

- QQ group / channel / DM send APIs
- Full event-specific ACK semantics beyond basic `{"op":12}` callback acknowledgement
- Group and other QQ event types
- WebSocket resume / shard orchestration beyond the minimal single-connection flow

## Confirmed Interface Rules

Based on the official QQ Bot docs for "接口调用与鉴权":

- Token endpoint: `POST https://bots.qq.com/app/getAppAccessToken`
- Request body fields: `appId`, `clientSecret`
- Token lifetime: up to `7200` seconds
- Renewal rule: when the current token is within `60` seconds of expiry, requesting again returns a new token while the old token remains valid during that `60` second overlap
- OpenAPI base URL: `https://api.sgroup.qq.com`
- Required auth header for OpenAPI requests: `Authorization: QQBot {ACCESS_TOKEN}`
- OpenAPI trace header: `X-Tps-trace-ID`

Based on the official QQ Bot docs for "事件订阅与通知":

- Webhook callbacks must use HTTPS in production
- Allowed callback ports are `80`, `443`, `8080`, `8443`
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

- `BUB_QQ_APPID`: QQ bot app ID
- `BUB_QQ_SECRET`: QQ bot secret
- `BUB_QQ_TOKEN_URL`: override token endpoint if needed
- `BUB_QQ_OPENAPI_BASE_URL`: override OpenAPI base URL if needed
- `BUB_QQ_TIMEOUT_SECONDS`: HTTP timeout for token and OpenAPI requests
- `BUB_QQ_TOKEN_REFRESH_SKEW_SECONDS`: token refresh lead time, defaults to `60`
- `BUB_QQ_RECEIVE_MODE`: `webhook` or `websocket`, defaults to `webhook`
- `BUB_QQ_WEBHOOK_HOST`: embedded webhook bind host, defaults to `127.0.0.1`
- `BUB_QQ_WEBHOOK_PORT`: embedded webhook bind port, defaults to `9009`
- `BUB_QQ_WEBHOOK_PATH`: webhook path, defaults to `/qq/webhook`
- `BUB_QQ_WEBHOOK_CALLBACK_TIMEOUT_SECONDS`: max time to wait for async event handling before returning HTTP 500
- `BUB_QQ_VERIFY_SIGNATURE`: whether to enforce webhook request signature validation, defaults to `true`
- `BUB_QQ_INBOUND_DEDUPE_SIZE`: recent `msg_id` cache size, defaults to `1024`
- `BUB_QQ_WEBSOCKET_INTENTS`: websocket identify intents, defaults to `1 << 25`
- `BUB_QQ_WEBSOCKET_USE_SHARD_GATEWAY`: whether to call `/gateway/bot`, defaults to `false`
- `BUB_QQ_WEBSOCKET_RECONNECT_DELAY_SECONDS`: reconnect delay after websocket disconnect, defaults to `5`
