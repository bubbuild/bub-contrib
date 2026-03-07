---
name: feishu
description: |
  Feishu Bot skill for sending messages, replying to specific messages, sending card messages,
  editing bot messages, and reacting to messages in Feishu/Lark chats.
metadata:
  channel: feishu
---

# Feishu Skill

Agent-facing execution guide for outbound communication in Feishu/Lark.

Assumption: `BUB_FEISHU_APP_ID` and `BUB_FEISHU_APP_SECRET` are already available.

## Required Inputs

Collect these fields before execution whenever possible:

- `chat_id`: required for sending a new message or card
- `message` / `text` / `content`: required for sending or editing content
- `message_id`: required for reply, edit, and reaction actions
- `chat_type`: used to determine whether the source is `p2p` or a group chat
- `mentions` / `parent_id` / `root_id`: used to detect thread context and explicit bot addressing

## Execution Policy

1. When handling the current Feishu conversation, if a user `message_id` is available, prefer reply semantics instead of sending an unrelated new message.
2. Prefer plain text for short, direct, conversational responses.
3. Prefer cards for Markdown content, status summaries, step lists, and structured updates.
4. Long-running tasks may send a short acknowledgment first, then use edits or follow-up messages for progress updates.
5. Only call scripts when a Feishu-specific platform action is required; otherwise return the final content directly.
6. When only lightweight acknowledgment is needed, prefer the Feishu message reaction API; if explanation or context is needed, send a normal reply instead.

## Active Response Policy

When this skill is in scope, prefer timely and proactive Feishu updates:

- Send a short acknowledgment when a new task starts
- Send progress updates for long-running work when appropriate
- Send a completion update when work finishes
- Send a problem report immediately when execution is blocked or fails
- If an acknowledgment has already been sent, prefer editing that message; send a new one only when editing is not appropriate

Recommended flow:

1. Send a short acknowledgment reply
2. Continue processing
3. If blocked, immediately edit the acknowledgment or send an issue update
4. Prefer editing to close the loop; otherwise send a final result message

## Reaction Policy

When an inbound Feishu message only needs a lightweight acknowledgment such as read, received, approved, or done, prefer a reaction.
When explanation, context, risk notes, result summaries, or next steps are needed, use a normal reply instead.

## Message Format Policy

- Short replies, confirmations, and direct answers: use `feishu_send.py --format text`
- Markdown content, progress summaries, checklists, and report-like output: use `feishu_send.py --format card`
- Updates to an existing bot message: use `feishu_edit.py`
- Lightweight acknowledgment: use the Feishu OpenAPI message reaction endpoint

## Special Message Policy

- In group chats, do not proactively join unless the bot is explicitly mentioned, replied to, or the message content includes `bub`.
- In group chats, if the bot is explicitly mentioned, replied to, or the current thread/reply chain already points to a previous bot message, continue within the same context.
- Treat `p2p` private chats as active by default and reply directly without requiring an explicit mention.
- When only lightweight acknowledgment is needed, prefer reactions; once explanation, risk notes, result summaries, or next steps are needed, switch to a normal reply.
- For thread messages, reply chains, and sequential status updates, prefer staying in the original context; when possible, close the loop by editing, otherwise send a follow-up message.
- Long-running tasks should follow an acknowledgment → progress → completion / blocked lifecycle so the user is not left without feedback.
- When blocked, failing, or waiting on an external dependency, send a problem report immediately, including failure point, completed work, impact, and next action.
- If `message_id` is missing, do not perform reply, edit, or reaction actions; if `chat_id` is missing, do not perform send actions.
- If card delivery is unsuitable or fails, fall back to `feishu_send.py --format text` so the message still reaches the user.

## Runtime Context Mapping

The current Feishu channel message JSON usually includes:

- `chat_id`: current conversation ID
- `message_id`: current user message ID
- `message`: normalized text content
- `chat_type`: `p2p` or group chat
- `sender_id` / `sender_open_id`
- `mentions`
- `parent_id`
- `root_id`
- `event_type`

Typical mappings:

- Send a new message to the current conversation: use `chat_id`
- Reply to the current user message: use `message_id` as the reply target
- Edit a previously sent bot message: use the target bot message `message_id`
- Add a reaction to the current message: use the current message `message_id`

## Command Templates

Paths are relative to this skill directory.

```bash
# Send text message
uv run ./scripts/feishu_send.py \
  --chat-id <CHAT_ID> \
  --content "<TEXT>" \
  --format text

# Reply to a specific message
uv run ./scripts/feishu_send.py \
  --chat-id <CHAT_ID> \
  --content "<TEXT>" \
  --format text \
  --reply-to <MESSAGE_ID>

# Send card update
uv run ./scripts/feishu_send.py \
  --chat-id <CHAT_ID> \
  --content "<MARKDOWN_CONTENT>" \
  --format card \
  --title "<TITLE>"

# Edit an existing bot message
uv run ./scripts/feishu_edit.py \
  --message-id <MESSAGE_ID> \
  --text "<TEXT>"
```

For actions not covered by the packaged scripts, such as reactions, call the Feishu OpenAPI directly.

## Script Interface Reference

### `feishu_send.py`

- `--chat-id`, `-c`: required
- `--content`, `-m`: required
- `--format`: optional, `text` or `card`, defaults to `text`
- `--title`, `-t`: used only with `--format card`
- `--reply-to`, `-r`: valid only with `--format text`
- `--app-id`: optional
- `--app-secret`: optional

### `feishu_edit.py`

- `--message-id`, `-m`: required
- `--text`, `-t`: required
- `--app-id`: optional
- `--app-secret`: optional

## API Docs

- Feishu Open Platform: `https://open.feishu.cn/`
- Feishu OpenAPI docs: `https://open.feishu.cn/document/`
- Lark Open Platform: `https://open.larksuite.com/`
- Lark OpenAPI docs: `https://open.larksuite.com/document/`
- For IM APIs, see the official `IM` / `Message` documentation
- Common endpoints:
  - `POST /open-apis/im/v1/messages`
  - `POST /open-apis/im/v1/messages/{message_id}/reply`
  - `PATCH /open-apis/im/v1/messages/{message_id}`
  - `POST /open-apis/im/v1/messages/{message_id}/reactions`

## Failure Handling

- If text or card sending fails, first check `chat_id`, message format, application permissions, and credentials.
- If card sending fails, fall back to `feishu_send.py --format text`.
- If editing fails, fall back to sending a new message and state that it is the updated result.
- If a reaction fails, fall back to a short text acknowledgment.
- If `message_id` is missing, do not perform reply, edit, or reaction actions.
- If `chat_id` is missing, do not perform send actions.
- If the task itself fails, do not report only the API error; also tell the user what failed, what was completed, the impact, and the next action.
