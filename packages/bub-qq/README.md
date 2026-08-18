# bub-qq

QQ Open Platform channel adapter for [Bub](https://bub.build).

Chinese documentation: [README.zh-CN.md](./README.zh-CN.md)

## What it provides

| Capability | Details |
| --- | --- |
| Single-chat (C2C) receive/reply | `C2C_MESSAGE_CREATE` adapted to Bub `ChannelMessage`; passive text / markdown replies |
| Group receive/reply | `GROUP_AT_MESSAGE_CREATE` / `GROUP_MESSAGE_CREATE`; full-message mode supported, payload carries `was_mentioned` / `sender_role` |
| Active group messages | Proactive fallback when a passive reply is impossible (`active_messages`, requires the group admin's opt-in in the QQ client) |
| Quotes and chat records | `msg_elements` parsed into `quoted_messages` (quoted messages / merged-forward chat records) for the model |
| Receive transport | **webhook** or **websocket** (mutually exclusive on the QQ platform side); ed25519 signature verification and reconnect included |
| Security | User/group allowlists, role-gated comma commands, per-scope tool policy, LLM rate limiting, audit logs (see Security) |
| Persisted platform state | Active-message opt-ins (`*_MSG_RECEIVE` / `*_MSG_REJECT`) and group claw_cfg survive restarts |
| Reliable sending | Inbound/outbound dedupe, `msg_seq` management, error catalog; async manual audit (304023/304024) treated as pending success |
| Onboarding | `bub onboard` collects `appid` / `secret` / `receive_mode`; bundled skill resources under `src/skills/qq` |

Plugin entry point: `qq` → `bub_qq.plugin`. Supports **single-chat (C2C)** and **group** text receive/reply. QQ Guild is not covered yet.

## Prerequisites

1. [Install Bub](https://bub.build/docs/getting-started/install/) (recommended: `uv tool install bub`)
2. Run `bub onboard` and ensure model access works (`bub chat` or `bub run`)
3. Create a QQ bot on the [QQ Open Platform](https://bot.q.qq.com/wiki/develop/api-v2/) and obtain `APPID` / `SECRET`

## Install (end users)

`bub-qq` is not on PyPI. With a global Bub install (`uv tool install bub`), install the plugin into **Bub’s own environment**:

```bash
bub install bub-qq@main
```

This resolves to the official monorepo package:

```text
git+https://github.com/bubbuild/bub-contrib.git@main#subdirectory=packages/bub-qq
```

Equivalent forms:

```bash
# Full Git URL (use https:// …, not git+https://, when passing to bub install)
bub install "https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-qq"

# Pin a tag or commit when available
bub install bub-qq@<tag-or-sha>
```

Verify the plugin is loaded:

```bash
bub hooks
```

You should see the `qq` plugin among discovered entry points / hook providers.

### Upgrade / uninstall

```bash
bub update bub-qq
bub uninstall bub-qq
```

### Notes

- Do **not** use bare `bub install bub-qq`. A name without `@ref` is treated as a PyPI package name.
- `bub install` requires Bub to run inside a virtual environment (including the environment created by `uv tool install bub`) and `uv` on `PATH`.

## Install (local development)

Editable install into the same environment that runs `bub`.

### Option A — global Bub (`uv tool`)

```bash
uv pip install -e /path/to/bub-contrib/packages/bub-qq \
  --python ~/.local/share/uv/tools/bub/bin/python
```

Then use the global CLI as usual:

```bash
bub hooks
bub gateway
```

### Option B — Bub / monorepo project venv

From a uv project that already depends on Bub:

```bash
uv add --editable /path/to/bub-contrib/packages/bub-qq
# or, from bub-contrib workspace workflows:
uv pip install -e packages/bub-qq
```

### Option C — raw Git install into a chosen interpreter

```bash
uv pip install \
  "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-qq" \
  --python /path/to/the/python/that/runs/bub
```

## Configuration

Settings can come from:

- the `qq:` section in `~/.bub/config.yml`
- `BUB_QQ_*` environment variables (including values loaded from `.env`)
- `bub onboard`, which interactively collects the required fields when the `qq` channel is enabled

Env vars override YAML, so shared policy can live in `config.yml` while secrets stay in the environment.

### Required

| YAML field (`qq.*`) | Env var | Description |
| --- | --- | --- |
| `appid` | `BUB_QQ_APPID` | QQ bot app ID |
| `secret` | `BUB_QQ_SECRET` | QQ bot secret |
| `receive_mode` | `BUB_QQ_RECEIVE_MODE` | Inbound transport: `webhook` or `websocket` |

`receive_mode` must match the QQ developer console:

- `webhook` — starts the embedded webhook server only; WebSocket is not started
- `websocket` — starts the WebSocket client only; the embedded webhook server is not started

QQ treats webhook and WebSocket as **mutually exclusive**. After a valid HTTPS webhook callback URL is configured successfully, WebSocket delivery is no longer supported on the platform side.

Gateway start fails if `appid` / `secret` are empty, or if `receive_mode` is not `webhook` / `websocket`.

### Optional

| YAML field (`qq.*`) | Env var | Default | Description |
| --- | --- | --- | --- |
| `token_url` | `BUB_QQ_TOKEN_URL` | `https://bots.qq.com/app/getAppAccessToken` | Access token endpoint |
| `openapi_base_url` | `BUB_QQ_OPENAPI_BASE_URL` | `https://api.bot.qq.com` | OpenAPI base URL (official unified endpoint; override here to use the legacy `https://api.sgroup.qq.com`) |
| `timeout_seconds` | `BUB_QQ_TIMEOUT_SECONDS` | `30` | HTTP timeout for token and OpenAPI |
| `token_refresh_skew_seconds` | `BUB_QQ_TOKEN_REFRESH_SKEW_SECONDS` | `60` | Refresh token this many seconds before expiry |
| `webhook_host` | `BUB_QQ_WEBHOOK_HOST` | `127.0.0.1` | Embedded webhook bind host |
| `webhook_port` | `BUB_QQ_WEBHOOK_PORT` | `8080` | Embedded webhook port (`80` / `443` / `8080` / `8443` allowed by QQ) |
| `webhook_path` | `BUB_QQ_WEBHOOK_PATH` | `/qq/webhook` | Webhook path |
| `webhook_callback_timeout_seconds` | `BUB_QQ_WEBHOOK_CALLBACK_TIMEOUT_SECONDS` | `15` | Reserved for future callback controls |
| `verify_signature` | `BUB_QQ_VERIFY_SIGNATURE` | `true` | Enforce webhook signature verification |
| `webhook_signature_timestamp_tolerance_seconds` | `BUB_QQ_WEBHOOK_SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS` | `0` | Reject webhook requests whose signature timestamp deviates from local time by more than this many seconds; `0` disables the freshness check |
| `inbound_dedupe_size` | `BUB_QQ_INBOUND_DEDUPE_SIZE` | `1024` | Recent inbound `msg_id` cache size |
| `session_state_size` | `BUB_QQ_SESSION_STATE_SIZE` | `1024` | Max sessions / send records kept in memory for passive replies (oldest entries are evicted) |
| `passive_reply_window_seconds` | `BUB_QQ_PASSIVE_REPLY_WINDOW_SECONDS` | `3600` | How long after an inbound message passive replies are attempted |
| `active_messages` | `BUB_QQ_ACTIVE_MESSAGES` | `false` | Send proactive group messages (no `msg_id`) when a passive reply is impossible; requires the group admin to allow proactive messages in the QQ client |
| `passive_replies_per_msg_id` | `BUB_QQ_PASSIVE_REPLIES_PER_MSG_ID` | `4` | Local cap of passive replies per inbound `msg_id`; beyond it the send falls back to an active message (when enabled) or is skipped |
| `state_file` | `BUB_QQ_STATE_FILE` | empty | JSON file persisting platform switches (active-message opt-ins, group claw_cfg); empty uses `<bub home>/qq/state.json` |
| `admin_users` | `BUB_QQ_ADMIN_USERS` | empty | Comma-separated user openids with full comma-command and tool access in every scope |
| `allow_users` | `BUB_QQ_ALLOW_USERS` | empty | Comma-separated C2C allowlist; when set, C2C messages from anyone else are dropped |
| `allow_groups` | `BUB_QQ_ALLOW_GROUPS` | empty | Comma-separated group allowlist; when set, messages from other groups are dropped |
| `group_tool_policy` | `BUB_QQ_GROUP_TOOL_POLICY` | `restricted` | Tool policy for group sessions: `open` / `restricted` (denies `bash*`, `fs.write`, `fs.edit`, `subagent`) / `locked` (denies all tools) |
| `c2c_tool_policy` | `BUB_QQ_C2C_TOOL_POLICY` | `open` | Tool policy for C2C sessions; same values as `group_tool_policy` |
| `denied_tools` | `BUB_QQ_DENIED_TOOLS` | empty | Extra comma-separated tool-name glob patterns denied under `restricted`, e.g. `web.fetch,tape.*` |
| `llm_rate_limit_per_minute` | `BUB_QQ_LLM_RATE_LIMIT_PER_MINUTE` | `0` | Max LLM calls per sender per session per minute; `0` disables |
| `llm_rate_limit_notice` | `BUB_QQ_LLM_RATE_LIMIT_NOTICE` | `请求过于频繁，请稍后再试。` | Reply text used when a sender hits the LLM rate limit |
| `websocket_intents` | `BUB_QQ_WEBSOCKET_INTENTS` | `1 << 25` | WebSocket identify intents (`GROUP_AND_C2C_EVENT`) |
| `websocket_use_shard_gateway` | `BUB_QQ_WEBSOCKET_USE_SHARD_GATEWAY` | `false` | Use `/gateway/bot` recommended shard count |
| `websocket_reconnect_delay_seconds` | `BUB_QQ_WEBSOCKET_RECONNECT_DELAY_SECONDS` | `5` | Delay before WebSocket reconnect |

Example:

```yaml
qq:
  appid: your_app_id
  secret: your_secret
  receive_mode: websocket
```

```bash
export BUB_QQ_APPID=your_app_id
export BUB_QQ_SECRET=your_secret
export BUB_QQ_RECEIVE_MODE=websocket
```

Which group messages the bot hears is controlled in the QQ client by a group admin setting (all messages / last 10 @mentions / @only). Every received group message wakes the model; `was_mentioned` in the payload is `false` when the bot was not @mentioned, and an empty reply is not sent.

Settings path in the latest mobile QQ client: **open the group chat → tap "More" in the top-right corner → Group Bots → Manage**. There the group owner or an admin can adjust the bot's group message scope and toggle "allow the bot to speak proactively" (pairs with `active_messages`).

## Security

The plugin ships with layered, fail-closed protections for public chats:

1. **Allowlists** — when `allow_users` / `allow_groups` are set, messages from anyone else are dropped before reaching the model.
2. **Comma-command gate** — inbound text starting with `,` runs as a Bub command only for authorized senders: in groups the platform-reported `member_role` must be `owner` / `admin`, or the sender must be in `admin_users`; in C2C only `admin_users` qualify. Everyone else's `,` message is forwarded as plain text.
3. **Tool policy** — a `before_tool_call` hook denies dangerous tools per scope. Groups default to `restricted` (no `bash*`, `fs.write`, `fs.edit`, `subagent`); C2C defaults to `open`. Authorized senders (rule 2) bypass the policy.
4. **Rate limit** — a `before_llm_call` hook caps LLM calls per sender per session (`llm_rate_limit_per_minute`) and short-circuits the turn with `llm_rate_limit_notice` when exceeded.
5. **Audit log** — `after_llm_call` / `after_tool_call` hooks emit `qq.audit.llm` / `qq.audit.tool` log lines with session, sender, role, tool/model, duration, and error type.

Note: with no configuration, comma commands are unusable in C2C (fail-closed). Set `admin_users` to your own openid to keep command access.

## Run

QQ is a channel listener surface. Start Bub gateway after the plugin is installed and configured:

```bash
bub gateway
```

For webhook mode, expose a public HTTPS URL that reaches the embedded server (host/port/path above) and register it in the QQ bot console. For websocket mode, ensure the console is **not** locked into a successful webhook-only configuration.

CLI chat (`bub chat`) does not replace the QQ channel; use gateway for QQ IO.

## Session and message mapping

| Concept | Format / behavior |
| --- | --- |
| Session ID (C2C) | `qq:c2c:<user_openid>` |
| Chat ID (C2C) | `c2c:<user_openid>` |
| Session ID (group) | `qq:group:<group_openid>` |
| Chat ID (group) | `group:<group_openid>` |
| Inbound event | `C2C_MESSAGE_CREATE`, `GROUP_AT_MESSAGE_CREATE`, `GROUP_MESSAGE_CREATE` |
| Group activation | every received group message is `is_active=true`; delivery scope is set in the QQ client by a group admin |
| Command messages | inbound text starting with `,` is forwarded as Bub `kind=command` for authorized senders only (see Security); otherwise treated as plain text |
| Outbound | Text (`msg_type = 0`), or markdown (`msg_type = 2`) when the reply looks like markdown; **passive reply preferred** (`msg_id` + plugin-managed `msg_seq`), with an optional **active fallback** for groups (`active_messages`, plain text only) |
| Passive window | passive replies stop once the latest inbound timestamp is older than 60 minutes; groups fall back to active messages when enabled |
| Active opt-in | `GROUP_MSG_RECEIVE` / `GROUP_MSG_REJECT` (and the C2C twins) are persisted per group/user; sends are skipped when the admin explicitly rejected active messages |
| Debounce | `needs_debounce = true` |

C2C stays passive-only: official docs state active C2C push stopped being provided on 2025-04-21. Group active messages are opt-in on both sides (bot config `active_messages` + the group admin's QQ client switch) and consume platform quota.

## Payload shape

Inbound non-command messages are encoded as a JSON string, including fields like:

- `message`
- `message_id`
- `type` (`text` or `attachment`)
- `sender_id` (C2C `user_openid`, group `member_openid`)
- `sender_name` / `sender_role` / `group_openid` / `chat_type` / `was_mentioned` (group)
- `date`
- `attachments` (when present)
- `quoted_messages` (when present: quoted message / merged-forward chat record content from `msg_elements`, with `message`, optional `sender_name`, and nested `messages`)

Normal replies should return final text and let Bub outbound routing call `QQChannel.send`. Do not call `qq_send.py` or invent `msg_seq` for ordinary C2C replies.

## Status

### Supported today

- Config via `qq:` YAML, `BUB_QQ_*`, and `bub onboard`
- Access token from `https://bots.qq.com/app/getAppAccessToken` with cached refresh (60s renewal window)
- `aiohttp` OpenAPI client with `Authorization: QQBot {ACCESS_TOKEN}`
- Embedded webhook receiver, callback validation (`op = 13`), ed25519 signature flows
- Webhook request verification (`X-Signature-Ed25519`, `X-Signature-Timestamp`)
- WebSocket receive path with reconnect / resume and optional sharding
- C2C / group inbound adaptation, `msg_id` dedupe, 60-minute passive text or markdown replies
- Group text receive/reply; message scope is controlled in the QQ client by a group admin
- In-memory send idempotency for the same `session_id + msg_id + msg_seq`
- OpenAPI error surfacing (HTTP status, platform `code` / `err_code`, trace_id from the response header or body) and error catalog metadata
- Layered security: allowlists, role-gated comma commands, per-scope tool policy, LLM rate limiting, and audit logs (see Security)
- Proactive group messages as a passive-reply fallback (`active_messages`), with persisted per-group/user opt-in state from `*_MSG_RECEIVE` / `*_MSG_REJECT` events
- claw_cfg round-trip: `INTERACTION_CREATE` 2002 updates are persisted per group and 2001 queries echo the real `require_mention` state
- 304023/304024 (async manual audit) treated as pending success instead of a failed send
- Automated tests for config, auth, signatures, channel, webhook, websocket, gateway, plugin onboarding, C2C/group services, security policies, and the platform store

### Not yet

- QQ Guild and rich-media send/receive
- Wider webhook event coverage beyond validation, basic `{"op":12}` ack, C2C/group messages, message-toggle events, and interaction query/update
- Active C2C push (discontinued by the platform on 2025-04-21)
- Markdown in active group messages (requires a registered template; active path sends plain text)
- Dynamic in-process shard rebalancing after startup

## Confirmed interface rules

From official QQ Bot docs (API auth + event subscription):

**Auth / OpenAPI**

- Token: `POST https://bots.qq.com/app/getAppAccessToken` body `{ appId, clientSecret }`
- Token lifetime up to `7200` seconds; refresh within `60` seconds of expiry returns a new token while the old remains valid during the overlap
- OpenAPI unified endpoint: `https://api.bot.qq.com` (the legacy `https://api.sgroup.qq.com` can be restored via `openapi_base_url`)
- Header: `Authorization: QQBot {ACCESS_TOKEN}`
- Failure response body carries `err_code`, `message`, `trace_id` (legacy format uses `code`; the plugin accepts both); trace_id is also exposed via the `X-Tps-trace-ID` response header

**Events / transport**

- Production webhooks require HTTPS; ports `80`, `443`, `8080`, `8443`
- Webhook and WebSocket are mutually exclusive once a valid HTTPS callback is configured
- Validation requests use `op = 13`; response must include `plain_token` and ed25519 signature over `event_ts + plain_token`
- Normal webhook verification uses `timestamp + raw_body`
- Event payload shape: `{ id, op, d, s, t }`
- `C2C_MESSAGE_CREATE` / `GROUP_AT_MESSAGE_CREATE` intent: `GROUP_AND_C2C_EVENT` (`1 << 25`)
- Documented `C2C_MESSAGE_CREATE.d` fields used here: `id`, `author.user_openid`, `content`, `timestamp`, `attachments`
- Group event `d` fields used here: `id`, `group_openid`, `author.member_openid`, `content`, `timestamp`, `mentions`, `attachments`
- Group send: `POST /v2/groups/{group_openid}/messages` with the same body as C2C (`msg_id`, `msg_seq`, plus either `content` + `msg_type = 0` or `markdown.content` + `msg_type = 2`)
- WebSocket close codes `4914` / `4915` are fatal; codes such as `4006`–`4009` and `4900`–`4913` are treated as reconnectable

## Official documentation

- [QQ Bot Developer Documentation](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/getting-started.html)
- [Bub docs](https://bub.build/)
- [Bub plugin hub](https://hub.bub.build/)

Use the QQ docs for app creation, credentials, event subscription, and callback settings (`APPID`, `SECRET`, webhook URL, intents, etc.).

## Development

```bash
uv run --package bub-qq pytest -q
```

Tests use mocks — no live QQ network required.

## License

Same as the [bub-contrib](https://github.com/bubbuild/bub-contrib) repository.
