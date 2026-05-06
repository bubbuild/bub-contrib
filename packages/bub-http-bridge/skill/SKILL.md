---
name: http-bridge
description: >
  Deliver messages or control commands (e.g. ,tape.handoff) into a specified bub session.
  Use when Bub needs to: (1) Inject a message into a specific session from an external process,
  (2) Send control commands to another session (cross-session), (3) Let a spawned subprocess
  report results back into the originating session.
---

# HTTP Bridge

Agent-facing guide for using the bub-http-bridge channel.

## When to Use

- Spawning an external process that needs to report results back or invoke framework capabilities
- Cross-session communication (e.g. posting from a telegram session into a feishu session)
- Providing callback instructions to child processes that cannot directly access bub tools

## Endpoint

```
POST http://127.0.0.1:9800/message
Content-Type: application/json
```

Default port is `9800`. Override via `BUB_HTTP_BRIDGE_PORT` env var.

## Obtaining session_id

The `session_id` format is `{channel}:{chat_id}`. Both values are available in the message context:

- `channel` — from context field `channel` (strip the `$` prefix)
- `chat_id` — from context field `chat_id`

Example: context shows `channel=$telegram|chat_id=5779521985` → session_id is `telegram:5779521985`

## Request Format

```json
{
  "session_id": "<channel>:<chat_id>",
  "content": "<message>",
  "source": "<caller_identity>"
}
```

- `session_id`: Target session. Format is `channel:chat_id` (e.g. `telegram:5779521985`, `feishu:xxx`).
- `content`: Message body. Prefix with `,` to execute as a bash command in that session. Plain text triggers normal agent processing.
- `source` (optional): Identifier of the calling process (e.g. `codex`, `deploy-script`). Defaults to `unknown`. Appears in context as `source=codex` so the agent knows the message is not from a human user.

## How the Agent Sees It

Messages posted via bridge arrive with `channel=$http-bridge` in context, not the target channel. This lets the agent distinguish bridge-injected messages from direct user messages. The `output_channel` is set to the target channel so responses route correctly.

Context example: `channel=$http-bridge|chat_id=5779521985|source=codex`

## Response

`202 Accepted` — fire-and-forget. The message is queued for processing.

## Usage Patterns

### Pattern 1: External process callback

When spawning a subprocess that should report back:

```bash
# Tell the child process how to call back
export BUB_CALLBACK="curl -s -X POST http://127.0.0.1:9800/message \
  -H 'Content-Type: application/json' \
  -d '{\"session_id\": \"telegram:5779521985\", \"content\": \"task done: result here\", \"source\": \"codex\"}'"
```

### Pattern 2: Execute a framework command from outside

```bash
curl -s -X POST http://127.0.0.1:9800/message \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "telegram:5779521985", "content": ",echo hello from external", "source": "deploy-script"}'
```

### Pattern 3: Cross-session messaging

```bash
curl -s -X POST http://127.0.0.1:9800/message \
  -H 'Content-Type: application/json' \
  -d '{"session_id": "feishu:group123", "content": "deployment complete", "source": "ci"}'
```

## Important Notes

- The bridge only listens on localhost — not exposed to the network.
- Messages posted via bridge go through the same processing pipeline as any channel message.
- The `,` command prefix convention works the same as in normal sessions.
- The target session must belong to an enabled channel for output routing to work.