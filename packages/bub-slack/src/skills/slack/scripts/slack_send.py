#!/usr/bin/env python3
"""
Slack message sender.

Posts a message to a Slack channel (optionally in a thread) via the Web API
using the bot token (BUB_SLACK_BOT_TOKEN). Prints the posted message's `ts` on
success so a follow-up `slack_edit.py` can update it later.

Stdlib-only (urllib) so it runs under the runtime venv python with no extra deps.
Reads message text from `--text -` (stdin) or an inline `--text` value.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SLACK_API = "https://slack.com/api/chat.postMessage"


def _read_text(value: str) -> str:
    # `-` means read the full message body from stdin (handles newlines safely).
    if value == "-":
        return sys.stdin.read()
    return value


def send_message(
    token: str,
    channel_id: str,
    text: str,
    thread_ts: str | None = None,
    blocks: list | None = None,
    mrkdwn: bool = True,
) -> dict:
    payload: dict = {"channel": channel_id, "text": text}
    if mrkdwn:
        payload["mrkdwn"] = True
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts

    req = urllib.request.Request(  # noqa: S310 — Slack HTTPS endpoint is fixed
        SLACK_API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Slack message via the Web API.")
    parser.add_argument("--channel-id", "-c", required=True, help="Target channel ID (e.g. C0XXXXXX).")
    parser.add_argument("--text", "-m", required=True, help='Message text (mrkdwn supported), or "-" to read from stdin.')
    parser.add_argument("--thread-ts", "-t", help="Parent message ts to post inside a thread.")
    parser.add_argument("--blocks-file", "-b", help="Path to a JSON file with a Block Kit `blocks` array.")
    parser.add_argument("--token", help="Bot token (defaults to BUB_SLACK_BOT_TOKEN env var).")
    parser.add_argument("--plain", action="store_true", help="Disable mrkdwn formatting.")
    args = parser.parse_args()

    token = args.token or os.environ.get("BUB_SLACK_BOT_TOKEN")
    if not token:
        print("❌ Error: bot token required. Set BUB_SLACK_BOT_TOKEN env var or use --token")
        sys.exit(1)

    text = _read_text(args.text)

    blocks = None
    if args.blocks_file:
        with open(args.blocks_file, encoding="utf-8") as fh:
            blocks = json.load(fh)

    try:
        result = send_message(
            token=token,
            channel_id=args.channel_id,
            text=text,
            thread_ts=args.thread_ts or None,
            blocks=blocks,
            mrkdwn=not args.plain,
        )
    except urllib.error.HTTPError as exc:
        print(f"❌ HTTP Error: {exc}\n   Response: {exc.read().decode('utf-8', 'replace')}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Error: {exc}")
        sys.exit(1)

    if not result.get("ok"):
        print(f"❌ Slack API error: {result.get('error')}")
        sys.exit(1)

    ts = result.get("ts", "")
    where = f"thread {args.thread_ts}" if args.thread_ts else args.channel_id
    print(f"✅ Sent to {where} (ts={ts})")


if __name__ == "__main__":
    main()
