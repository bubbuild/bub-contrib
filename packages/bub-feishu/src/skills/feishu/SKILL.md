---
name: feishu
description: |
  Use this skill to respond to messages from feishu channel.
---

# Feishu Skill

Agent-facing execution guide for outbound communication in Feishu/Lark.

Assumption: `BUB_FEISHU_APP_ID` and `BUB_FEISHU_APP_SECRET` are already available.

## Required Inputs

Collect these fields before execution whenever possible:

- `chat_id`: required for sending a new message or card; obtain it from the current channel/session context, not from the inbound JSON payload
- `message` / `text` / `content`: required for sending or editing content
- `message_id`: required for reply, edit, and reaction actions
- `reply_to_message.message_id`: optional source reference from inbound metadata; use it only for context, not as a replacement for the current message id

## Execution Policy

1. If handling an active Feishu conversation and `message_id` is known, prefer reply semantics instead of sending an unrelated new message.
2. If source metadata says sender is a bot (`sender_is_bot=true`), do not assume reply mode is appropriate; prefer a normal message unless there is a clear user-authored message to reply to.
3. Prefer plain text for short, direct, conversational responses.
4. Prefer cards for Markdown content, status summaries, step lists, and structured updates.
5. For long-running tasks, send one acknowledgment first, then prefer edits or one follow-up update to close the loop.
6. For multi-line text passed through shell commands, prefer heredoc command substitution instead of embedding raw line breaks in quoted strings.
7. Only call scripts when a Feishu-specific platform action is required; otherwise return the final content directly.
8. When only lightweight acknowledgment is needed, prefer the Feishu message reaction API; if explanation or context is needed, use a normal reply instead.
9. Do not assume the Feishu channel will send replies automatically; all platform actions must go through the Feishu scripts or direct OpenAPI calls.


## Message Format Policy

- Short replies, confirmations, and direct answers: use `feishu_send.py --format text`
- Markdown content, progress summaries, checklists, and report-like output: use `feishu_send.py --format card`
- Updates to an existing bot message: use `feishu_edit.py`
- Lightweight acknowledgment: use the Feishu OpenAPI message reaction endpoint

## Special Message Policy

- Respect the current runtime context prepared by the channel: only act when the current message has already reached the agent.
- If `sender_is_bot=true`, prefer a normal message unless a reply target is explicitly required and known to be correct.
- When only lightweight acknowledgment is needed, prefer reactions; once explanation, risk notes, result summaries, or next steps are needed, switch to a normal reply.
- For reply chains and sequential status updates, prefer staying in the original context; when possible, close the loop by editing, otherwise send a follow-up message.
- Long-running tasks should follow an acknowledgment → progress → completion / blocked lifecycle so the user is not left without feedback.
- When blocked, failing, or waiting on an external dependency, send a problem report immediately, including failure point, completed work, impact, and next action.
- If `message_id` is missing, do not perform reply, edit, or reaction actions; if `chat_id` is missing, do not perform send actions.
- If card delivery is unsuitable or fails, fall back to `feishu_send.py --format text` so the message still reaches the user.

## Runtime Context Mapping

The current Feishu channel message JSON includes:

- `message`: normalized text content
- `message_id`: current user message ID
- `type`: normalized message type
- `sender_id`
- `sender_is_bot`
- `date`
- `reply_to_message`

Typical mappings:

- Send a new message to the current conversation: use `chat_id` from the current channel/session context
- Reply to the current user message: use `message_id` as the reply target
- Edit a previously sent bot message: use the target bot message `message_id`
- Add a reaction to the current message: use the current message `message_id`

## Command Templates

Paths are relative to this skill directory.

```bash
# Send text message
uv run ${SKILL_DIR}/scripts/feishu_send.py \
  --chat-id <CHAT_ID> \
  --content "<TEXT>" \
  --format text

# Send multi-line text message (heredoc)
uv run ${SKILL_DIR}/scripts/feishu_send.py \
  --chat-id <CHAT_ID> \
  --content "$(cat <<'EOF'
Build finished successfully.
Summary:
- 12 tests passed
- 0 failures
EOF
)" \
  --format text

# Reply to a specific message
uv run ${SKILL_DIR}/scripts/feishu_send.py \
  --chat-id <CHAT_ID> \
  --content "<TEXT>" \
  --format text \
  --reply-to <MESSAGE_ID>

# Send card update
uv run ${SKILL_DIR}/scripts/feishu_send.py \
  --chat-id <CHAT_ID> \
  --content "<MARKDOWN_CONTENT>" \
  --format card \
  --title "<TITLE>"

# Edit an existing bot message
uv run ${SKILL_DIR}/scripts/feishu_edit.py \
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

- Feishu OpenAPI docs: `https://open.feishu.cn/document/`
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
- If reply sending fails, fall back to sending a normal message to the same `chat_id`.
- If editing fails, fall back to sending a new message and state that it is the updated result.
- If a reaction fails, fall back to a short text acknowledgment.
- If `message_id` is missing, do not perform reply, edit, or reaction actions.
- If `chat_id` is missing, do not perform send actions.
- If the task itself fails, do not report only the API error; also tell the user what failed, what was completed, the impact, and the next action.
