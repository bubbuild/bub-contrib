#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "wecom-aibot-python-sdk>=1.0.2",
# ]
# ///

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import sys
from collections.abc import Awaitable
from typing import Any, cast

from aibot import WSClient, WSClientOptions


def _register_handler(client: WSClient, event: str, handler: Any) -> None:
    registration = getattr(client, "on")
    try:
        registration(event, handler)
    except TypeError:
        decorator = registration(event)
        decorator(handler)


def _prepare_auth_waiter(client: WSClient) -> tuple[asyncio.Event, list[BaseException]]:
    authenticated = asyncio.Event()
    errors: list[BaseException] = []

    def on_authenticated() -> None:
        authenticated.set()

    def on_error(error: Exception) -> None:
        errors.append(error)
        authenticated.set()

    def on_disconnected(reason: str) -> None:
        errors.append(RuntimeError(f"WeCom websocket disconnected before authentication: {reason}"))
        authenticated.set()

    _register_handler(client, "authenticated", on_authenticated)
    _register_handler(client, "error", on_error)
    _register_handler(client, "disconnected", on_disconnected)
    return authenticated, errors


async def _wait_for_authentication(
    authenticated: asyncio.Event,
    errors: list[BaseException],
    timeout_seconds: float = 10.0,
) -> None:
    await asyncio.wait_for(authenticated.wait(), timeout=timeout_seconds)
    if errors:
        raise errors[0]


async def _disconnect_client(client: WSClient) -> None:
    result = client.disconnect()
    if inspect.isawaitable(result):
        await cast(Awaitable[Any], result)


async def send_message(
    bot_id: str,
    secret: str,
    chat_id: str,
    content: str,
    *,
    message_format: str = "markdown",
    websocket_url: str | None = None,
) -> None:
    body_key = "markdown" if message_format == "markdown" else "text"
    client = WSClient(
        WSClientOptions(
            bot_id=bot_id,
            secret=secret,
            ws_url=websocket_url or "wss://openws.work.weixin.qq.com",
            max_reconnect_attempts=0,
        )
    )
    authenticated, errors = _prepare_auth_waiter(client)
    await client.connect()
    try:
        await _wait_for_authentication(authenticated, errors)
        await client.send_message(
            chat_id,
            {
                "msgtype": message_format,
                body_key: {"content": content},
            },
        )
        await asyncio.sleep(0.2)
    finally:
        await _disconnect_client(client)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a proactive message to WeCom")
    parser.add_argument("--chat-id", "-c", required=True, help="Target WeCom chat ID")
    parser.add_argument("--content", "-m", required=True, help="Content to send")
    parser.add_argument(
        "--format",
        choices=("markdown", "text"),
        default="markdown",
        help="Outbound WeCom message format",
    )
    parser.add_argument(
        "--bot-id",
        default=os.environ.get("BUB_WECOM_BOT_ID"),
        help="WeCom bot ID",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("BUB_WECOM_SECRET"),
        help="WeCom bot secret",
    )
    parser.add_argument(
        "--websocket-url",
        default=os.environ.get("BUB_WECOM_WEBSOCKET_URL"),
        help="WeCom websocket URL",
    )
    args = parser.parse_args()

    if not args.bot_id or not args.secret:
        print("Error: BUB_WECOM_BOT_ID and BUB_WECOM_SECRET are required")
        sys.exit(1)

    try:
        asyncio.run(
            send_message(
                args.bot_id,
                args.secret,
                args.chat_id,
                args.content,
                message_format=args.format,
                websocket_url=args.websocket_url,
            )
        )
        print(f"Message sent to {args.chat_id}")
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
