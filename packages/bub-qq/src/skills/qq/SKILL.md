---
name: qq
description:
  QQ C2C channel skill. Use when Bub is handling a QQ conversation and must send a reply
  explicitly instead of relying on framework auto-delivery.
metadata:
  channel: qq
---

# QQ Skill

Use this skill when the current conversation is on QQ and the framework does not auto-send replies.

## Execution Policy

- QQ currently supports C2C passive replies only.
- Do not assume a normal text return will be delivered automatically.
- To reply to the current QQ user, call the packaged send script with:
  - `openid`: use `sender_id` from the inbound QQ JSON payload
  - `msg_id`: use `message_id` from the inbound QQ JSON payload
  - `content`: the final text to send
- Prefer `msg_seq=1` for a single direct reply to the current inbound message.
- If `sender_id` or `message_id` is missing, do not attempt to send.

## Context Mapping

Current QQ inbound message JSON typically includes:

- `message`: normalized text content
- `message_id`: QQ inbound message id for passive reply
- `sender_id`: QQ user openid
- `date`
- `attachments`

## Command Template

Paths are relative to this skill directory.

```bash
uv run python ./scripts/qq_send.py \
  --openid <SENDER_ID> \
  --msg-id <MESSAGE_ID> \
  --content "<TEXT>" \
  --msg-seq 1
```

## Response Contract

- When replying to a QQ user, send the QQ message first using the script above.
- After the send succeeds, end the turn without restating a conflicting claim such as "QQ skill is unavailable".
