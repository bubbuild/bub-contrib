---
name: slack
description: |
  Slack skill for proactive outbound Slack communication: posting a message to a
  channel or thread, editing an existing bot message, or adding an emoji reaction.
  Use when the bot needs to: (1) post a message somewhere OTHER than the current
  reply (e.g. a different channel, or a side note in the same thread),
  (2) edit a previously-sent bot message (e.g. a progress placeholder), or
  (3) acknowledge a message with a non-verbal emoji reaction.
  NOTE: normal replies to the current message are posted AUTOMATICALLY by the
  runtime — just answer as text. Reach for this skill only for proactive /
  out-of-band sends and message edits.
metadata:
  channel: slack
---

# Slack Skill

Agent-facing execution guide for **proactive** Slack outbound communication.

Your ordinary reply to the user is delivered automatically — you do **not** call
this skill to answer. Use it only for: proactive sends (another channel/thread),
editing a bot message you already posted, or reacting with an emoji.

> The runtime also **auto-acks** every inbound message for you: it reacts ⏳
> (`:hourglass:`) on receipt and flips to ✅ (`:white_check_mark:`) once you
> reply. Do **not** add those reactions yourself for the current turn — this
> skill is only for proactive / out-of-band reactions.

## Env vars

- `BUB_SLACK_BOT_TOKEN` — the bot OAuth token (`xoxb-...`). The runtime sets it
  in the process environment at startup. The scripts read it from the
  environment; you do **not** need to pass it or expose its value anywhere.

The runtime resolves `$PYTHON` (the venv interpreter) and `$SKILL_DIR` (this
skill's directory) for you in every example below — run the commands verbatim.
The scripts are stdlib-only (no `requests`), so they work under the venv python
with no extra dependencies.

## Where you are

Your runtime context surfaces Slack identity you can reuse for proactive sends:

- `chat_id` — the current channel ID (e.g. `C0XXXXXX`)
- `thread_ts` — the current thread's parent ts (present inside a thread; blank at channel root)
- `ts` — the inbound message's own ts

To post a proactive message **in the same thread**, pass `--thread-ts <thread_ts>`.
To post at channel root, omit `--thread-ts`.

## Send a message

```bash
$PYTHON $SKILL_DIR/scripts/slack_send.py \
  --channel-id <CHANNEL_ID> \
  --thread-ts <THREAD_TS>   # optional; omit for channel root
  --text -                  # "-" reads the body from stdin
```

```bash
printf '*Heads up*\nHere is a proactive note.' | \
  $PYTHON $SKILL_DIR/scripts/slack_send.py --channel-id C0XXXXXX --text -
```

The script prints the new message's `ts` (e.g. `Sent to ... (ts=1234.0005)`) —
capture it to edit the message later.

### Block Kit (optional, richer formatting)

For structured output (sections, fields, buttons), pass a `blocks` JSON file:

```bash
$PYTHON $SKILL_DIR/scripts/slack_send.py --channel-id C0XXXXXX --blocks-file blocks.json --text "fallback text"
```

Always include a plain `--text` fallback (shown in notifications) alongside blocks.
Full Block Kit reference: https://api.slack.com/block-kit

## Edit a message

```bash
$PYTHON $SKILL_DIR/scripts/slack_edit.py \
  --channel-id <CHANNEL_ID> \
  --ts <TS_OF_BOT_MESSAGE> \
  --text -
```

Only messages **authored by the bot** can be edited.

## React to a message

```bash
$PYTHON $SKILL_DIR/scripts/slack_react.py \
  --channel-id <CHANNEL_ID> \
  --ts <MESSAGE_TS> \
  --reaction white_check_mark   # emoji name, no colons
```

Common reactions: `eyes` (looking at it), `hourglass` (working), `white_check_mark`
(done), `rocket` (shipped).

## Progress-update pattern (long tasks)

For long-running work, borrow the send-then-edit model:

1. Post a short acknowledgement in the thread and capture its `ts`:
   ```bash
   TS=$(printf 'Working on it…' | $PYTHON $SKILL_DIR/scripts/slack_send.py \
        --channel-id <CHANNEL_ID> --thread-ts <THREAD_TS> --text - \
        | sed -n 's/.*ts=\([0-9.]*\).*/\1/p')
   ```
2. Add an `:hourglass:` reaction or edit the placeholder with progress.
3. Do the work.
4. Edit the placeholder with the final result:
   ```bash
   printf '*Done.*\n<result>' | $PYTHON $SKILL_DIR/scripts/slack_edit.py \
     --channel-id <CHANNEL_ID> --ts "$TS" --text -
   ```

Prefer editing the placeholder over posting many incremental messages.

## Formatting

- Text uses Slack `mrkdwn` by default (`*bold*`, `_italic_`, `` `code` ``, ``` ```block``` ```).
- Keep proactive messages short and skimmable.
- For tables or complex layouts, use Block Kit instead of ascii art.
