#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx>=0.27.0",
# ]
# ///

import argparse
import json
import os
import sys
from typing import Any

from feishu_utils import get_tenant_access_token, request_json


def edit_text_message(app_id: str, app_secret: str, message_id: str, text: str) -> dict[str, Any]:
    token = get_tenant_access_token(app_id, app_secret)
    return request_json(
        "PATCH",
        f"/im/v1/messages/{message_id}",
        token=token,
        payload={"content": json.dumps({"text": text}, ensure_ascii=False)},
    )


def edit_message(app_id: str, app_secret: str, message_id: str, text: str) -> dict[str, Any]:
    return edit_text_message(app_id, app_secret, message_id, text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Edit a Feishu message")
    parser.add_argument("--message-id", "-m", required=True)
    parser.add_argument("--text", "-t", required=True)
    parser.add_argument("--app-id", default=os.environ.get("BUB_FEISHU_APP_ID"))
    parser.add_argument("--app-secret", default=os.environ.get("BUB_FEISHU_APP_SECRET"))
    args = parser.parse_args()

    if not args.app_id or not args.app_secret:
        print("Error: BUB_FEISHU_APP_ID and BUB_FEISHU_APP_SECRET are required")
        sys.exit(1)

    result = edit_message(args.app_id, args.app_secret, args.message_id, args.text)
    if result.get("code") != 0:
        print(f"Error: {result.get('msg')}")
        sys.exit(1)
    print(f"Message updated: {args.message_id}")


if __name__ == "__main__":
    main()
