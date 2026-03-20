#!/usr/bin/env python3

from __future__ import annotations

import asyncio

import typer

from bub_qq.auth import QQTokenProvider
from bub_qq.config import QQConfig
from bub_qq.openapi import QQOpenAPI


app = typer.Typer(add_completion=False)


@app.command()
def main(
    openid: str = typer.Option(..., "--openid", help="QQ user_openid for the current C2C conversation"),
    content: str = typer.Option(..., "--content", help="Reply text to send"),
    msg_id: str = typer.Option(..., "--msg-id", help="Inbound QQ message id to reply to"),
    msg_seq: int = typer.Option(1, "--msg-seq", help="Reply sequence number for the current inbound message"),
) -> None:
    asyncio.run(_send(openid=openid, content=content, msg_id=msg_id, msg_seq=msg_seq))


async def _send(*, openid: str, content: str, msg_id: str, msg_seq: int) -> None:
    config = QQConfig()
    if not config.appid or not config.secret:
        raise RuntimeError("qq appid/secret is empty")

    provider = QQTokenProvider(config)
    openapi = QQOpenAPI(config, provider)
    try:
        result = await openapi.post_c2c_text_message(
            openid=openid,
            content=content,
            msg_id=msg_id,
            msg_seq=msg_seq,
        )
    finally:
        await openapi.aclose()
    print(result.get("id", "(no id)"))


if __name__ == "__main__":
    app()
