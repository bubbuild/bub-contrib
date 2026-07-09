#!/usr/bin/env python3
"""
Slack message editor.

Updates the text (and optionally the blocks) of an existing message authored by
the bot, identified by channel + ts. Used for the send-then-edit progress-update
pattern: post a short acknowledgement, do the work, then edit it with the result.

Stdlib-only (urllib) so it runs under the runtime venv python with no extra deps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SLACK_API = "https://slack.com/api/chat.update"


def _read_text(value: str) -> str:
    if value == "-":
        return sys.stdin.read()
    return value


def edit_message(
    token: str,
    channel_id: str,
    ts: str,
    text: str,
    blocks: list | None = None,
    mrkdwn: bool = True,
) -> dict:
    payload: dict = {"channel": channel_id, "ts": ts, "text": text}
    if mrkdwn:
        payload["mrkdwn"] = True
    if blocks:
        payload["blocks"] = blocks

    req = urllib.request.Request(  # noqa: S310 — Slack HTTPS endpoint is fixed
        SLACK_API,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Edit an existing Slack bot message via the Web API."
    )
    parser.add_argument(
        "--channel-id", "-c", required=True, help="Channel ID of the message to edit."
    )
    parser.add_argument(
        "--ts", required=True, help="Timestamp (ts) of the message to edit."
    )
    parser.add_argument(
        "--text",
        "-m",
        required=True,
        help='New message text (mrkdwn supported), or "-" to read from stdin.',
    )
    parser.add_argument(
        "--blocks-file",
        "-b",
        help="Path to a JSON file with a Block Kit `blocks` array.",
    )
    parser.add_argument(
        "--token", help="Bot token (defaults to BUB_SLACK_BOT_TOKEN env var)."
    )
    parser.add_argument(
        "--plain", action="store_true", help="Disable mrkdwn formatting."
    )
    args = parser.parse_args()

    token = args.token or os.environ.get("BUB_SLACK_BOT_TOKEN")
    if not token:
        print(
            "❌ Error: bot token required. Set BUB_SLACK_BOT_TOKEN env var or use --token"
        )
        sys.exit(1)

    text = _read_text(args.text)

    blocks = None
    if args.blocks_file:
        with open(args.blocks_file, encoding="utf-8") as fh:
            blocks = json.load(fh)

    try:
        result = edit_message(
            token=token,
            channel_id=args.channel_id,
            ts=args.ts,
            text=text,
            blocks=blocks,
            mrkdwn=not args.plain,
        )
    except urllib.error.HTTPError as exc:
        print(
            f"❌ HTTP Error: {exc}\n   Response: {exc.read().decode('utf-8', 'replace')}"
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Error: {exc}")
        sys.exit(1)

    if not result.get("ok"):
        print(f"❌ Slack API error: {result.get('error')}")
        sys.exit(1)

    print(f"✅ Edited message {args.ts} in {args.channel_id}")


if __name__ == "__main__":
    main()
