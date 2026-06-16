#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31.0",
# ]
# ///

"""
DingTalk message sender script.
Send text/markdown messages to DingTalk groups or users via Robot API.
"""

import argparse
import json
import os
import sys
from typing import Any

import requests

OPENAPI_BASE = "https://api.dingtalk.com"
OAPI_BASE = "https://oapi.dingtalk.com"
TOKEN_URL = f"{OPENAPI_BASE}/v1.0/oauth2/accessToken"


def get_access_token(client_id: str, client_secret: str) -> str:
    """Get DingTalk access token."""
    resp = requests.post(
        TOKEN_URL,
        json={"appKey": client_id, "appSecret": client_secret},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("accessToken")
    if not token:
        raise RuntimeError(f"Failed to get token: {data.get('message', 'unknown')}")
    return str(token)


def send_message(
    client_id: str,
    client_secret: str,
    chat_id: str,
    content: str,
    *,
    title: str = "Bub Reply",
    msg_key: str = "sampleMarkdown",
) -> dict[str, Any]:
    """Send a markdown message to DingTalk."""
    return _send_robot_message(
        client_id,
        client_secret,
        chat_id,
        msg_key,
        {"text": content, "title": title},
    )


def upload_media(
    client_id: str,
    client_secret: str,
    file_bytes: bytes,
    filename: str,
    mime_type: str = "image/png",
) -> str:
    """Upload an image to DingTalk and return its media_id.

    Uses the legacy ``/media/upload`` endpoint (multipart/form-data, field name
    ``media``). The returned ``media_id`` is reusable and can be passed as the
    ``photoURL`` of a ``sampleImageMsg`` robot message. See:
    https://open.dingtalk.com/document/orgapp/upload-media-files
    """
    token = get_access_token(client_id, client_secret)
    resp = requests.post(
        f"{OAPI_BASE}/media/upload",
        params={"type": "image", "access_token": token},
        files={"media": (filename, file_bytes, mime_type)},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json() if resp.text else {}
    errcode = data.get("errcode")
    if errcode not in (None, 0):
        raise RuntimeError(
            f"DingTalk media upload failed: errcode={errcode} "
            f"errmsg={data.get('errmsg', '')}"
        )
    media_id = data.get("media_id")
    if not media_id:
        raise RuntimeError(f"DingTalk media upload returned no media_id: {data}")
    return str(media_id)


def send_image_message(
    client_id: str,
    client_secret: str,
    chat_id: str,
    photo_ref: str,
) -> dict[str, Any]:
    """Send a ``sampleImageMsg`` robot message.

    ``photo_ref`` may be either a ``media_id`` returned by :func:`upload_media`
    or a publicly reachable HTTPS URL. Local file paths are not supported here;
    upload them first.
    """
    return _send_robot_message(
        client_id,
        client_secret,
        chat_id,
        "sampleImageMsg",
        {"photoURL": photo_ref},
    )


def _send_robot_message(
    client_id: str,
    client_secret: str,
    chat_id: str,
    msg_key: str,
    msg_param: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a robot message to either group or 1:1 based on chat_id prefix."""
    token = get_access_token(client_id, client_secret)
    headers = {
        "Content-Type": "application/json",
        "x-acs-dingtalk-access-token": token,
    }

    if chat_id.startswith("group:"):
        url = f"{OPENAPI_BASE}/v1.0/robot/groupMessages/send"
        payload = {
            "robotCode": client_id,
            "openConversationId": chat_id[6:],
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }
    else:
        url = f"{OPENAPI_BASE}/v1.0/robot/oToMessages/batchSend"
        payload = {
            "robotCode": client_id,
            "userIds": [chat_id],
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }

    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    result = resp.json() if resp.text else {}
    errcode = result.get("errcode")
    if errcode not in (None, 0):
        raise RuntimeError(
            f"DingTalk send failed: errcode={errcode} msg={result.get('message', '')}"
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Send message to DingTalk")
    parser.add_argument(
        "--chat-id", "-c", required=True, help="Target chat ID (group:xxx or user_id)"
    )
    parser.add_argument(
        "--content", "-m", required=True, help="Message content (markdown)"
    )
    parser.add_argument("--title", "-t", default="Bub Reply", help="Message title")
    parser.add_argument(
        "--client-id",
        default=os.environ.get("BUB_DINGTALK_CLIENT_ID"),
        help="DingTalk app client_id",
    )
    parser.add_argument(
        "--client-secret",
        default=os.environ.get("BUB_DINGTALK_CLIENT_SECRET"),
        help="DingTalk app client_secret",
    )
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print(
            "Error: BUB_DINGTALK_CLIENT_ID and BUB_DINGTALK_CLIENT_SECRET are required"
        )
        sys.exit(1)

    try:
        send_message(
            args.client_id,
            args.client_secret,
            args.chat_id,
            args.content,
            title=args.title,
        )
        print(f"Message sent to {args.chat_id}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
