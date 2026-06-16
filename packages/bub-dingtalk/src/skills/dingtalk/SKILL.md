---
name: dingtalk
description: |
  DingTalk channel skill. When $dingtalk appears in message context, return your response as text
  and the framework will deliver it. For programmatic sends (e.g. progress updates), use dingtalk_send.
metadata:
  channel: dingtalk
---

# DingTalk Skill

When the message context contains `$dingtalk` and `chat_id`, you are in a DingTalk conversation.

## Primary: Return Your Response Directly

**To reply to a DingTalk user, simply return your response as text.** The framework will automatically deliver it to the correct conversation. No script call is required.

- The `chat_id` from the message context is used automatically.
- Just write your answer and end the turn. The user will receive it.

Example: User asks "What is 2+2?" → You return "4" → The framework sends "4" to DingTalk.

## Sending Images

To include a local image in your reply, use markdown image syntax with a local
file path:

```
Here's the chart:
![sales](/tmp/sales_report.png)
```

The framework will:

1. Strip the markdown from the text (so the user just sees "Here's the chart:").
2. Upload the local file to DingTalk via the media upload API.
3. Send the image as a separate `sampleImageMsg` after the text.

Rules:

- **Absolute local paths** (e.g. `/tmp/result.png`) and `file://` URIs are uploaded.
- **http(s)/data URLs are left as-is** in the markdown text — DingTalk's markdown
  renderer decides whether to render them. If you need a guaranteed-delivered
  image, prefer a local path (and let the framework upload it).
- Supported formats: `jpg`, `gif`, `png`, `bmp`. Max 20MB per image.
- Missing files are skipped (the markdown stays in the text and a warning is
  logged); other replies continue to send.

## When to Use the Script

Use `dingtalk_send` only when you need to send **from within a tool** (e.g. a progress update during a long-running task, or a status message triggered by another tool).

```bash
uv run ./scripts/dingtalk_send.py --chat-id <CHAT_ID> --content "<TEXT>"
```

Get `chat_id` from the message context JSON (the `chat_id` field). For group chats it is `group:<openConversationId>`, for 1:1 it is the user's staff_id.

## Context Fields

When `$dingtalk` is in scope, the message context typically includes:

- `channel`: `$dingtalk` — identifies this as a DingTalk conversation
- `chat_id`: target for sending (e.g. `204818006723348842` for 1:1, or `group:xxx` for groups)

## Response Policy

- **Direct answers**: Return your response. The framework delivers it.
- **Long-running tasks**: Return a short acknowledgment first (e.g. "Working on it..."), then continue. When done, return the final result.
- **Errors**: Return an error message so the user sees it immediately.

## Script Reference (for programmatic use)

```bash
uv run ./scripts/dingtalk_send.py \
  --chat-id <CHAT_ID> \
  --content "<TEXT>" \
  [--title "<TITLE>"]
```

- `--chat-id`, `-c`: required
- `--content`, `-m`: required
- `--title`, `-t`: optional, default "Bub Reply"
