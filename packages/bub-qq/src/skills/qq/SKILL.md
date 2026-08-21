---
name: qq
description:
  QQ C2C and group channel skill. Use when Bub is handling a QQ conversation. In the default
  direct reply mode, return your normal text reply directly and let the QQ channel deliver it
  through standard Bub outbound routing; output exactly <no_reply/> to stay silent. In tool
  reply mode, reply by calling the qq.send tool and stay silent by not calling it.
metadata:
  channel: qq
---

# QQ Skill

Use this skill when the current conversation is on QQ.

## Execution Policy

- QQ supports C2C and group **passive** replies (`msg_id` + plugin-managed `msg_seq`), with an optional active-message fallback in groups.
- The reply contract depends on the `qq.reply_mode` config; the active mode is stated in the system prompt (`<qq_response_instruct>`):
  - **direct** (default): return the final text directly. Bub standard outbound will route it to `QQChannel.send`. To stay silent, output exactly `<no_reply/>` and nothing else.
  - **tool**: call the `qq.send` tool with the message text. Your plain final text is not delivered. To stay silent, do not call `qq.send`.
- Do not construct or pass `msg_seq`; QQ reply sequencing is managed inside the plugin.
- If the current QQ payload is missing `sender_id` or `message_id`, do not invent protocol fields or shell commands.
- Empty replies are skipped.

## Group Chat

- Session is per group (`qq:group:<group_openid>`), not per sender.
- Inbound JSON includes `chat_type=group`, `group_openid`, `sender_id` (member openid), `sender_name`, and `was_mentioned`.
- You are woken for every group message QQ delivers. A group admin controls that scope in the QQ client (all messages, last 10 @mentions, or @only).
- When `was_mentioned` is false, reply only if you have something useful to add; otherwise stay silent (per the active reply mode above).
- Prefer human messages over other bots. Do not compete or spam the group.

## Context Mapping

Current QQ inbound message JSON typically includes:

- `message`: normalized text content (`<@bot>` mentions are stripped)
- `message_id`: QQ inbound message id for passive reply
- `sender_id`: C2C `user_openid`, or group `member_openid`
- `sender_name`: group nickname when present
- `group_openid` / `chat_type`: present for group messages
- `was_mentioned`: whether this group message @mentioned the bot
- `date`
- `attachments`

## Response Contract

- When replying to a QQ user or group, return the final reply text and end the turn.
- Do not describe shell commands, script paths, or `msg_seq` handling in the answer.

## Markdown

QQ C2C and group replies can render native markdown. Prefer this syntax so emphasis
shows as formatting instead of literal `**asterisks**`:

- headings: `#` `##` `###`
- `**bold**`, `*italic*`, `~~strikethrough~~`
- unordered/ordered lists, `>` quotes, `***` horizontal rules
- links `[text](https://example.com)` and public-image links

Avoid GFM tables and fenced code blocks. QQ may reject those; the plugin then
falls back to a plain-text message.
