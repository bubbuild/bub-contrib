#!/usr/bin/env python3
"""
Slack reaction adder.

Adds an emoji reaction to a message (channel + ts). Useful for lightweight,
non-verbal acknowledgement — e.g. react with 👀 when starting a long task,
:white_check_mark: when done — without posting a new message.

Reaction names are Slack emoji names without surrounding colons, e.g.
`eyes`, `white_check_mark`, `hourglass`, `rocket`.

Stdlib-only (urllib) so it runs under the runtime venv python with no extra deps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

SLACK_API = "https://slack.com/api/reactions.add"


def add_reaction(token: str, channel_id: str, ts: str, name: str) -> dict:
    payload = {"channel": channel_id, "timestamp": ts, "name": name}
    req = urllib.request.Request(  # noqa: S310 — Slack HTTPS endpoint is fixed
        SLACK_API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Add an emoji reaction to a Slack message.")
    parser.add_argument("--channel-id", "-c", required=True, help="Channel ID of the message.")
    parser.add_argument("--ts", required=True, help="Timestamp (ts) of the message.")
    parser.add_argument("--reaction", "-r", required=True, help="Emoji name without colons (e.g. white_check_mark).")
    parser.add_argument("--token", help="Bot token (defaults to BUB_SLACK_BOT_TOKEN env var).")
    args = parser.parse_args()

    token = args.token or os.environ.get("BUB_SLACK_BOT_TOKEN")
    if not token:
        print("❌ Error: bot token required. Set BUB_SLACK_BOT_TOKEN env var or use --token")
        sys.exit(1)

    name = args.reaction.strip().strip(":")
    try:
        result = add_reaction(token=token, channel_id=args.channel_id, ts=args.ts, name=name)
    except urllib.error.HTTPError as exc:
        print(f"❌ HTTP Error: {exc}\n   Response: {exc.read().decode('utf-8', 'replace')}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Error: {exc}")
        sys.exit(1)

    if not result.get("ok"):
        # `already_reacted` is benign — the reaction is already present.
        if result.get("error") == "already_reacted":
            print(f"✅ Already reacting :{name}: on {args.ts}")
            return
        print(f"❌ Slack API error: {result.get('error')}")
        sys.exit(1)

    print(f"✅ Reacted :{name}: on {args.ts} in {args.channel_id}")


if __name__ == "__main__":
    main()
