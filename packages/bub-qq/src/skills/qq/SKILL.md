---
name: qq
description:
  QQ C2C channel skill. Use when Bub is handling a QQ conversation. Return your normal text
  reply directly and let the QQ channel deliver it through standard Bub outbound routing.
metadata:
  channel: qq
---

# QQ Skill

Use this skill when the current conversation is on QQ.

## Execution Policy

- QQ currently supports C2C passive replies only.
- For a normal QQ reply, return the final text directly. Bub standard outbound will route it to `QQChannel.send`.
- Do not call `qq_send.py` for normal replies.
- Do not construct or pass `msg_seq`; QQ reply sequencing is managed inside the plugin.
- If the current QQ payload is missing `sender_id` or `message_id`, do not invent protocol fields or shell commands.

## Context Mapping

Current QQ inbound message JSON typically includes:

- `message`: normalized text content
- `message_id`: QQ inbound message id for passive reply
- `sender_id`: QQ user openid
- `date`
- `attachments`

## Response Contract

- When replying to a QQ user, return the final reply text and end the turn.
- Do not describe shell commands, script paths, or `msg_seq` handling in the answer.
