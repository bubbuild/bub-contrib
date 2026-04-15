---
name: wecom
description:
  WeCom channel skill. Return normal replies directly. For proactive sends such as scheduled jobs,
  use the packaged wecom_send.py script.
metadata:
  channel: wecom
---

# WeCom Skill

Use this skill when the current conversation is on WeCom.

## Execution Policy

- Normal replies in the current WeCom conversation: return the final text directly.
- Proactive sends outside the current turn, such as scheduled jobs, tool notifications, and progress updates: call `wecom_send.py`.
- Use `chat_id` from runtime context when available. If only `session_id` exists, derive it from `wecom:<chat_id>`.
- Keep the response text-first. Markdown is supported.
- Do not construct `req_id`, `stream_id`, or raw WebSocket payloads.

## Current Context

Current WeCom message JSON may include:

- `message`
- `message_id`
- `message_type`
- `sender_id`
- `chat_type`
- `quote`

## Script Usage

```bash
uv run ${SKILL_DIR}/scripts/wecom_send.py \
  --chat-id <CHAT_ID> \
  --content "<TEXT>"
```

Multi-line content:

```bash
uv run ${SKILL_DIR}/scripts/wecom_send.py \
  --chat-id <CHAT_ID> \
  --content "$(cat <<'EOF'
Task finished.
- 12 checks passed
- 0 failures
EOF
)"
```
