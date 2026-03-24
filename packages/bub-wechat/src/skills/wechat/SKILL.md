---
name: wechat
description:
  WeChat channel skill. Use when Bub is handling a WeChat conversation. Return your
  normal text reply directly and let the WeChat channel deliver it through standard
  Bub outbound routing.
metadata:
  channel: wechat
---

# WeChat Skill

Use this skill when the current conversation is on WeChat.

## Execution Policy

- For a normal WeChat reply, return the final text directly. Bub standard outbound will route it to `WeChatChannel.send`.
- Do not call any active-send tool for normal replies.
- Do not construct or pass protocol fields such as context tokens in the answer.

## Context Mapping

Current WeChat inbound message JSON typically includes:

- `message`: normalized text content
- `message_id`: WeChat inbound message id or a stable fallback fingerprint
- `sender_id`: WeChat user id
- `date`
- `attachments`

## Response Contract

- When replying to a WeChat user, return the final reply text and end the turn.
- Do not describe shell commands, script paths, or protocol details in the answer.
