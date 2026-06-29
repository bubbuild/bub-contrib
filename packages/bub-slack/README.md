# bub-slack

A Slack **Socket Mode** channel adapter for [Bub](https://bub.build/). Registered
as a standalone Bub plugin (entry-point group `bub`, name `slack`), it is
auto-discovered by any Bub distribution that depends on it.

Socket Mode receives events over a WebSocket (the `xapp-` token) with no public
HTTP endpoint; replies are posted via the Web API (the `xoxb-` bot token). Uses
only the official `slack_sdk` package.

## Install

```bash
uv pip install bub-slack          # from PyPI / a workspace
uv pip install -e packages/bub-slack   # editable, in this workspace
```

## Configuration

Slack settings can come from:

- the `slack:` section in `~/.bub/config.yml`
- `BUB_SLACK_*` env vars (including values loaded from `.env`)

Env vars override YAML, so deployments can keep shared policy such as
allow-lists in `config.yml` while reserving env/Secrets for tokens.

| YAML field (`slack.*`) | Env var | Purpose |
| ---------------------- | ------- | ------- |
| `bot_token` | `BUB_SLACK_BOT_TOKEN` | Bot OAuth token |
| `app_token` | `BUB_SLACK_APP_TOKEN` | App-level token |
| `allow_channels` | `BUB_SLACK_ALLOW_CHANNELS` | Allowed channel IDs |
| `allow_users` | `BUB_SLACK_ALLOW_USERS` | Allowed user IDs |

The channel is only `enabled` when **both** tokens are present, so `bub gateway`
silently skips it on a machine that only does `bub run` / `bub chat`.

## Behavior

- In channels (public/private) the bot reacts when **@-mentioned**, or when a
  message is a reply inside a thread the bot has already posted into (so a help
  thread does not need a fresh `@mention` on every turn). In DMs it reacts to
  every message.
- Messages from bots (including itself) and message subtypes (joins, edits,
  deletes, file shares, ...) are ignored to avoid echo loops and noise.
- **Sessions are thread-aware.** DMs are keyed `slack:{channel_id}` (continuous
  private memory). Shared channels are keyed `slack:{channel_id}:{thread_root}`,
  where `thread_root` is the thread's parent ts for a reply, or the message ts
  for a top-level post. Two threads in the same channel — and two top-level
  mentions — get independent short-term memory and are never merged by the
  debounce/coalesce layer.
- **Replies stay in the originating thread.** Outbound `send()` recovers the
  thread ts from the inbound context, falling back to the thread-scoped
  `session_id` when Bub's outbound renderer has dropped the context — so a
  threaded reply is always posted in-thread, never back at channel root.
- Long replies are chunked at Slack's 4000-char limit and posted in-thread when
  the inbound message had a `thread_ts`.
- **Inbound ack.** Every accepted message is acknowledged with a ⏳
  (`:hourglass:`) reaction on receipt, flipped to ✅ (`:white_check_mark:`) once
  the bot posts its reply. Reaction failures are non-fatal (logged at debug) and
  never block message handling; requires the `reactions:write` bot scope.

## Slack app prerequisites

1. Create a Slack app; under **Socket Mode**, turn the toggle **on** and
   generate an **App-Level Token** (`xapp-...`) with the
   `connections:write` scope.
2. **OAuth & Permissions** → Bot Token Scopes: `chat:write`,
   `app_mentions:read`, `im:history`, `channels:history`, `groups:history`,
   `reactions:write` (used to ack inbound messages with ⏳ → ✅).
3. **Event Subscriptions** → Subscribe to bot events: `message.channels`,
   `message.groups`, `message.im`, `app_mention`.
4. Install the app to the workspace and invite the bot to the target channel.

## Development

```bash
uv run --package bub-slack pytest -q
```

Tests use mocks — no live Slack network required.
