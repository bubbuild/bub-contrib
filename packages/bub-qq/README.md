# bub-qq

QQ Open Platform channel adapter for [Bub](https://bub.build).

Chinese documentation: [README.zh-CN.md](./README.zh-CN.md)

## What it provides

| Capability | Details |
| --- | --- |
| Single-chat (C2C) receive/reply | `C2C_MESSAGE_CREATE` adapted to Bub `ChannelMessage`; passive text / markdown / rich-media replies |
| Group receive/reply | `GROUP_AT_MESSAGE_CREATE` / `GROUP_MESSAGE_CREATE`; full-message mode supported, payload carries `was_mentioned` / `sender_role` |
| Active group messages | Proactive fallback when a passive reply is impossible (`active_messages`, requires the group admin's opt-in in the QQ client) |
| Reply modes & selective silence | `reply_mode: tool` (default) exposes a native `qq.send` tool so the model replies by calling it and stays silent by not calling it; `direct` forwards the model's final text and swallows `<no_reply/>` (see Reply modes) |
| Quotes and chat records | `msg_elements` parsed into `quoted_messages` (quoted messages / merged-forward chat records) for the model |
| Receive transport | **webhook** or **websocket** (mutually exclusive on the QQ platform side); ed25519 signature verification and reconnect included |
| Security | One Guard for every action: admin pairing (`,qq.claim`), per-resource rules, one-time shell approvals, protected files, public-only downloads, allowlists, rate limiting, audit logs (see Security) |
| Persisted platform state | Active-message opt-ins (`*_MSG_RECEIVE` / `*_MSG_REJECT`) and group claw_cfg survive restarts |
| Reliable sending | Inbound/outbound dedupe, `msg_seq` management, error catalog; async manual audit (304023/304024) treated as pending success |
| Onboarding | `bub onboard` collects `appid` / `secret` / `receive_mode`; bundled skill resources under `src/skills/qq` |

Plugin entry point: `qq` → `bub_qq.plugin`. Supports **single-chat (C2C)** and **group** text receive/reply. QQ Guild is not covered yet.

## Prerequisites

1. [Install Bub](https://bub.build/docs/getting-started/install/) 0.6.0 or later (recommended: `uv tool install bub`). On an older Bub the plugin does not load and the log says why. Newer minor releases and development builds load with a `qq.bub.untested` / `qq.bub.prerelease` warning: tools a newer Bub adds stay admin-only until bub-qq knows them
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
| `verify_signature` | `BUB_QQ_VERIFY_SIGNATURE` | `true` | Enforce webhook signature verification |
| `webhook_signature_timestamp_tolerance_seconds` | `BUB_QQ_WEBHOOK_SIGNATURE_TIMESTAMP_TOLERANCE_SECONDS` | `300` | Reject webhook requests whose signature timestamp deviates from local time by more than this many seconds (blocks replayed callbacks; keep the server clock in sync); `0` disables the check. Webhook bodies over 1 MB are rejected |
| `inbound_dedupe_size` | `BUB_QQ_INBOUND_DEDUPE_SIZE` | `1024` | Recent inbound `msg_id` cache size |
| `session_state_size` | `BUB_QQ_SESSION_STATE_SIZE` | `1024` | Max sessions / send records kept in memory for passive replies (oldest entries are evicted) |
| `passive_reply_window_seconds` | `BUB_QQ_PASSIVE_REPLY_WINDOW_SECONDS` | platform limit | Override how long after an inbound message passive replies are attempted; unset uses the platform limits (3600 s in C2C, 300 s in groups) |
| `active_messages` | `BUB_QQ_ACTIVE_MESSAGES` | `false` | Send proactive group messages (no `msg_id`) when a passive reply is impossible; requires the group admin to allow proactive messages in the QQ client |
| `passive_replies_per_msg_id` | `BUB_QQ_PASSIVE_REPLIES_PER_MSG_ID` | platform limit | Override the local cap of passive replies per inbound message; unset uses the platform limits (4 in C2C, 5 in groups). Beyond it the send falls back to an active message (when enabled) or is skipped |
| `group_wake` | `BUB_QQ_GROUP_WAKE` | `all` | Which group messages start a model turn: `all` (the model decides whether to reply) or `mention` (only @-mentions; follow-ups right after are still included). `mention` saves model calls in busy groups |
| `reply_mode` | `BUB_QQ_REPLY_MODE` | `tool` | How model output reaches QQ: `tool` (default) disables direct forwarding and exposes the `qq.send` tool; `direct` forwards the final text (output exactly `<no_reply/>` to stay silent) (see Reply modes) |
| `state_file` | `BUB_QQ_STATE_FILE` | empty | JSON file persisting registered admins and platform switches (active-message opt-ins, group claw_cfg); empty uses `<bub home>/qq/state.json` |
| `admin_users` | `BUB_QQ_ADMIN_USERS` | empty | Admins, the only trusted senders. Entries are scoped identities (`c2c:<user_openid>`, `group:<group_openid>:<member_openid>`) or bare openids matching in any scope. Usually left empty and filled by `,qq.claim` (see Security) |
| `allow_users` | `BUB_QQ_ALLOW_USERS` | empty | Comma-separated C2C allowlist; when set, C2C messages from anyone else are dropped |
| `allow_groups` | `BUB_QQ_ALLOW_GROUPS` | empty | Comma-separated group allowlist; when set, messages from other groups are dropped |
| `group_shell` | `BUB_QQ_GROUP_SHELL` | `approval` | Shell (`bash`) for admins: `approval` (an admin taps to confirm every command; no always-allow) or `deny`. Non-admins never get a shell. Applies in groups and C2C |
| `shell_sandbox` | `BUB_QQ_SHELL_SANDBOX` | `none` | Declare whether Bub runs inside an external sandbox (`none` / `external`). The plugin does not isolate commands; `none` with shell enabled logs a warning at startup |
| `c2c_access` | `BUB_QQ_C2C_ACCESS` | `admin_users` | Who may use tools in C2C: `admin_users`, or `allow_users` (requires a non-empty `allow_users`; startup fails otherwise). Replies always work |
| `download_allow_hosts` | `BUB_QQ_DOWNLOAD_ALLOW_HOSTS` | empty | Comma-separated hostnames that `media_url`, `web.fetch` and attachment downloads may reach even on private addresses (e.g. an intranet image host) |
| `group_tool_policy` | `BUB_QQ_GROUP_TOOL_POLICY` | `restricted` | Policy for tools other than shell and files in groups: `open` / `restricted` (denies `subagent` and `denied_tools`) / `locked` (denies every tool except replies, admins included) |
| `c2c_tool_policy` | `BUB_QQ_C2C_TOOL_POLICY` | `open` | Same, for C2C users allowed by `c2c_access` |
| `allowed_tools` | `BUB_QQ_ALLOWED_TOOLS` | empty | Extra comma-separated tool-name glob patterns non-admins may use, e.g. `web_search.*`. Tools outside the built-in list and these patterns are admin-only; shell, code mode and session-changing tools are unaffected |
| `denied_tools` | `BUB_QQ_DENIED_TOOLS` | empty | Extra comma-separated tool-name glob patterns denied under `restricted`, e.g. `web.fetch,tape.*` |
| `llm_rate_limit_per_minute` | `BUB_QQ_LLM_RATE_LIMIT_PER_MINUTE` | `0` | Max LLM calls per sender per session per minute; `0` disables |
| `llm_rate_limit_notice` | `BUB_QQ_LLM_RATE_LIMIT_NOTICE` | `请求过于频繁，请稍后再试。` | Reply text used when a sender hits the LLM rate limit |
| `websocket_intents` | `BUB_QQ_WEBSOCKET_INTENTS` | `1 << 25` | WebSocket identify intents (`GROUP_AND_C2C_EVENT`) |
| `websocket_use_shard_gateway` | `BUB_QQ_WEBSOCKET_USE_SHARD_GATEWAY` | `false` | Use `/gateway/bot` recommended shard count |
| `websocket_reconnect_delay_seconds` | `BUB_QQ_WEBSOCKET_RECONNECT_DELAY_SECONDS` | `5` | Base delay before WebSocket reconnect (doubles per consecutive failure) |
| `websocket_reconnect_max_delay_seconds` | `BUB_QQ_WEBSOCKET_RECONNECT_MAX_DELAY_SECONDS` | `300` | Upper bound for the exponential reconnect backoff |
| `websocket_max_identify_rejections` | `BUB_QQ_WEBSOCKET_MAX_IDENTIFY_REJECTIONS` | `5` | Stop the client after this many consecutive identify rejections (op 9); `0` disables the limit |

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

Removed in 0.3.0: `exec_approval` (use `group_shell`) and `workspace_jail` (replaced by the Guard; see Security). If still set they are ignored with a startup warning.

Which group messages the bot hears is controlled in the QQ client by a group admin setting (all messages / last 10 @mentions / @only). With `group_wake: all` (default) every received group message wakes the model, and `was_mentioned` in the payload is `false` when the bot was not @mentioned; with `group_wake: mention` only @-mentions start a turn.

Settings path in the latest mobile QQ client: **open the group chat → tap "More" in the top-right corner → Group Bots → Manage**. There the group owner or an admin can adjust the bot's group message scope and toggle "allow the bot to speak proactively" (pairs with `active_messages`).

## Reply modes

`reply_mode` decides how model output becomes a QQ message, and — just as important — how the model stays silent (e.g. for un-mentioned group chatter it has nothing to add to). In opt-in/opt-out terms: `direct` is **opt-out** (replying is the default; the model explicitly opts out with a sentinel), while `tool` is **opt-in** (silence is the default; the model explicitly opts in by calling the send tool — the same contract Bub's other channels use). Both modes share the same send pipeline (passive `msg_id`/`msg_seq` targeting, dedupe, markdown fallback, active-message fallback), and a per-mode `<qq_response_instruct>` block is injected into the system prompt so the model knows the active contract.

### `tool` (default)

Direct forwarding is disabled (model output is routed to the `null` channel) and a native `qq.send` tool is registered instead. The model replies by calling `qq.send` with the message text (and optionally `media_url` / `file_type` for native rich media) — `msg_id`/`msg_seq` are resolved internally, so the model never touches protocol fields — and stays silent by simply not calling it. Buttons are plugin-owned, not model-generated. The failure mode: if the model forgets to call the tool, the reply is silently lost.

### `direct`

The model's final text is forwarded to the chat as-is — delivery never depends on the model calling anything. To skip a reply, the model outputs exactly `<no_reply/>`; the channel swallows it (logged as `qq.send skip_no_reply`) and nothing is sent. Leaked model special tokens (`<|eos|>`, `<|im_end|>`, …) are stripped from the edges of outbound text; output consisting only of such tokens is also treated as silence. Use this when the configured model's tool-calling reliability is unknown: the failure mode is an unwanted message, never a lost one. `direct` cannot send `media_url`.

`qq.send` is exempt from the tool policy (`group_tool_policy` / `c2c_tool_policy` / `denied_tools`): it is the reply path itself, which was never gated in direct mode.

Notes for `tool` mode:

- Comma-command output is always delivered directly in both modes (commands bypass the model).
- When a sender hits `llm_rate_limit_per_minute`, the plugin sends `llm_rate_limit_notice` itself (at most once per minute per sender). The limit counts turns, not the individual LLM calls of a multi-step turn.
- `qq.send` replies to the message that triggered the turn, even if newer messages arrived meanwhile; button clicks are answered through `event_id` as QQ requires.

## Security

Every action a chat can trigger passes one checkpoint, the **Guard**: model tool calls and comma commands (both in `before_tool_call`, since Bub runs a command's tool with the tool hooks), and approved executions. It decides from who is asking and which resource the call touches. Anyone who can message the bot can steer the model, so model tool calls are treated as coming from the sender.

**Who is trusted.** Only admins: identities in `admin_users` plus those registered with `,qq.claim`. When Bub merges several messages into one turn (it debounces bursts), the turn counts as an admin's only if every sender in it is an admin, so a member's request cannot ride on an admin's message sent right after it. A turn that reaches a QQ session without a QQ sender (a scheduled job, another plugin) is checked as an unknown member, never as an admin, even in a private chat (log: `qq.security.unattributed_turn`). QQ group owner/admin roles grant nothing, because whoever deploys the bot is not necessarily the group owner. The same person has a different openid in C2C and in each group, so identities are scoped (`c2c:<user_openid>`, `group:<group_openid>:<member_openid>`).

**What each resource allows** (defaults):

| Resource | Everyone else | Admins |
| --- | --- | --- |
| shell (`bash`) | denied | approval for every command (`group_shell`) |
| read files (`fs.read`) | inside the workspace | inside the workspace |
| write files (`fs.write` / `fs.edit`) | denied | inside the workspace |
| protected files (`.env*`, `.git/`, the state file) | denied | denied |
| send a file (`qq.send media_path`) | only `outbox/` and `inbox/` | same |
| downloads (`media_url`, `web.fetch`, attachments) | public internet only, every redirect checked | same |
| code mode (`run_code` / `code_mode`) | denied | denied |
| read-only tools (`tape.info` / `tape.search` / `tape.anchors` / `tape.handoff`, `skill`, `tool.describe`, `spill.read`, `help`, `qq.fetch_attachment`) | `group_tool_policy` / `c2c_tool_policy` | same |
| session-changing tools (`tape.reset`, `subagent`, `model`, `reasoning_effort`, `quit`) | denied | `group_tool_policy` / `c2c_tool_policy` |
| unlisted tools (other plugins, MCP, tools new in Bub) | denied unless matched by `allowed_tools` | `group_tool_policy` / `c2c_tool_policy` |

In C2C, tools are admin-only by default (`c2c_access`); replies always work.

**Approval.** An admin's shell command posts a card with the full command (commands too long to show in full are refused, never truncated) and two buttons, clickable only by that group's registered admins. A tap on allow issues a one-time token bound to the session, requester, tool and exact arguments, valid for 240 seconds; the command then re-enters the Guard to use it. The card shows the command itself (other arguments on a `#` line) and flags destructive patterns such as `rm -rf`, `find -delete`, `dd of=` or `curl | sh` with a warning; the warning is a hint, approval stays the admin's call. There is no always-allow. Once approved, model-issued calls and admins' comma commands alike are handed back to Bub as a comma command (a model call is first rewritten as the equivalent `,bash command=…`), so they run in the original session's execution environment, post their output to the chat and land on the session tape. Bub keeps command output out of the model's context, so how each approval ended (`ran` with its output, `denied`, `expired`, `refused`, `failed`) is added once to the session's next message as `approval_results` (the last 3, output cut at 500 characters).

**Shell is not sandboxed by the plugin.** Nothing in bub-qq confines what an approved command does. Run Bub in a container or similar and set `shell_sandbox=external`, or set `group_shell=deny`.

**Becoming an admin.** On first start with no admins, the log prints a one-time pairing code:

```
qq.admin.unclaimed no admins configured; send ',qq.claim K7QX2M9PHT' to the bot in a private (C2C) chat within 10 minutes to become its admin
```

1. Add the bot as a friend and send `,qq.claim <code>` in C2C. A code sent in a group is burned and replaced.
2. To act as admin in a group, send `,qq.claim group` in C2C, then post the returned group code (valid 1 minute, single use) in that group. Whoever asked for the code gets a private message when it is used, naming the identity it registered, so a code someone else replayed first can be revoked with `,qq.admins remove`.
3. `,qq.admins` lists admins; `,qq.admins remove <identity>` removes a registered one.

Five wrong codes lock that sender out for 10 minutes; the bot says so once and then ignores their claims until the lock ends, so nobody can make it flood the chat. Claim and admin commands are handled by the plugin and never reach the model or the tape. You can skip pairing by writing identities into `admin_users`.

Also in place: allowlists (`allow_users` / `allow_groups`) drop other chats before the model; `llm_rate_limit_per_minute` caps LLM calls per sender; `qq.audit.llm` / `qq.audit.tool` log lines record every call (`requested_model` in `qq.audit.llm` is the model asked for; with `fallback_models`, the trace's `chat` span shows the one that answered).

### Ops comma commands

The command prefix follows Bub's `BUB_COMMAND_PREFIX` (`,` by default and in the examples below). Comma commands are accepted only from admins; anyone else's `,` message reaches the model as plain text. Each command is checked by the Guard exactly as Bub will run it. Ops commands are registered with `exposure="command"`, so they never appear in the model's tool list.

| Command | Description |
| --- | --- |
| `,qq.claim <code>` / `,qq.claim group` | Register as admin (see above); accepted from anyone |
| `,qq.admins` / `,qq.admins remove <identity>` | List or remove admins |
| `,qq.version` | Show the installed bub-qq plugin version |

Any other registered Bub tool can be run as `,name args`; an unknown `,name` is run by Bub as a shell line and therefore follows the shell rule.

## Run

QQ is a channel listener surface. Start Bub gateway after the plugin is installed and configured:

```bash
bub gateway
```

The workspace is pwd at start. Prefer a dedicated directory, so downloaded attachments land in `inbox/` and plugin downloads in `outbox/` at its root:

```bash
mkdir -p ~/Documents/bub-qq-work
cd ~/Documents/bub-qq-work
bub gateway
```

If you start from `/` or `$HOME` (or a directory that is not writable), those folders move to `~/.bub/qq/` instead of the filesystem root. File tools still stay inside the process working directory.

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
| Outbound | Default `qq.send`: text / markdown (`msg_type = 0/2`), rich media (`msg_type = 7`; plugin downloads `media_url` then multipart-uploads, or workspace `media_path`); **passive reply preferred** (`msg_id` + plugin-managed `msg_seq`), with an optional **active fallback** for groups (`active_messages`, plain text only — rich media does not take the active path) |
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
- `message_type` (0=text, 3=ARK card, 101/102/103=quote or chat record)
- `ark_data` (card payload when `message_type` is 3)

Attachments are not downloaded on arrival. When the model needs one it calls `qq.fetch_attachment(message_id, index)`, which saves it under `inbox/<message_id>/` (only for messages from the same chat).

In `direct` mode, normal replies should return final text and let Bub outbound routing call `QQChannel.send`; in `tool` mode, replies go through the `qq.send` tool. In both cases `msg_seq` is managed inside the plugin — never invent protocol fields.

## Status

### Supported today

- Config via `qq:` YAML, `BUB_QQ_*`, and `bub onboard`
- Access token from `https://bots.qq.com/app/getAppAccessToken` with cached refresh (60s renewal window)
- `aiohttp` OpenAPI client with `Authorization: QQBot {ACCESS_TOKEN}`
- Embedded webhook receiver, callback validation (`op = 13`), ed25519 signature flows
- Webhook request verification (`X-Signature-Ed25519`, `X-Signature-Timestamp`)
- WebSocket receive path with reconnect / resume and optional sharding
- C2C / group inbound adaptation, `msg_id` dedupe, 60-minute passive text or markdown replies
- `qq.send` rich media: download `media_url` (public addresses only) or use `media_path` (`outbox/` / `inbox/` only), multipart upload, then `msg_type = 7` (C2C/group isolated; passive window only)
- On-demand attachment download with `qq.fetch_attachment`
- Plugin-owned message buttons (fixed templates); `INTERACTION_CREATE` type 11/12 is PUT-acked then adapted inbound
- Group text receive/reply; message scope is controlled in the QQ client by a group admin
- In-memory send idempotency for the same `session_id + msg_id + msg_seq`
- OpenAPI error surfacing (HTTP status, platform `code` / `err_code`, trace_id from the response header or body) and error catalog metadata
- Guard-based security: admin pairing, per-resource rules, one-time approvals for shell, protected files, public-only downloads, allowlists, LLM rate limiting, and audit logs (see Security)
- Proactive group messages as a passive-reply fallback (`active_messages`), with persisted per-group/user opt-in state from `*_MSG_RECEIVE` / `*_MSG_REJECT` events
- claw_cfg round-trip: `INTERACTION_CREATE` 2002 updates are persisted per group and 2001 queries echo the real `require_mention` state
- 304023/304024 (async manual audit) treated as pending success instead of a failed send
- Selective silence in both reply modes: `<no_reply/>` sentinel filtering (`direct`) and reply-by-tool with silence-by-omission (`tool`), driven by an injected per-mode system prompt block
- Automated tests for config, auth, signatures, channel, webhook, websocket, gateway, plugin onboarding, C2C/group services, security policies, reply modes, and the platform store

### Not yet

- QQ Guild
- Sandboxing shell commands (declare an external sandbox with `shell_sandbox`)
- Wider webhook event coverage beyond validation, basic `{"op":12}` ack, C2C/group messages, message-toggle events, interaction query/update, and button/menu clicks
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
- Group send: `POST /v2/groups/{group_openid}/messages` with the same body as C2C (`msg_id`, `msg_seq`, plus `content` + `msg_type = 0`, `markdown.content` + `msg_type = 2`, or `media.file_info` + `msg_type = 7`)
- Rich-media upload: local multipart (`upload_prepare` / `upload_part_finish` / `files` merge with `upload_id`) then `msg_type = 7`; C2C and group files cannot be mixed
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
