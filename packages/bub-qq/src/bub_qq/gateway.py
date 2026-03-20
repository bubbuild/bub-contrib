from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .openapi import QQOpenAPI


@dataclass(frozen=True)
class QQGatewayInfo:
    url: str
    shards: int | None = None
    max_concurrency: int | None = None


async def get_gateway(openapi: QQOpenAPI) -> QQGatewayInfo:
    payload = await openapi.get("/gateway")
    return QQGatewayInfo(url=str(payload["url"]))


async def get_shard_gateway(openapi: QQOpenAPI) -> QQGatewayInfo:
    payload = await openapi.get("/gateway/bot")
    limit = payload.get("session_start_limit")
    max_concurrency = None
    if isinstance(limit, dict):
        value = limit.get("max_concurrency")
        if value is not None:
            max_concurrency = int(value)
    return QQGatewayInfo(
        url=str(payload["url"]),
        shards=int(payload["shards"]) if payload.get("shards") is not None else None,
        max_concurrency=max_concurrency,
    )


def identify_payload(*, token: str, intents: int, shard: tuple[int, int] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "token": f"QQBot {token}",
        "intents": intents,
        "properties": {
            "$os": "macos",
            "$browser": "bub-qq",
            "$device": "bub-qq",
        },
    }
    if shard is not None:
        data["shard"] = [shard[0], shard[1]]
    return {"op": 2, "d": data}


def resume_payload(*, token: str, session_id: str, sequence: int) -> dict[str, Any]:
    return {
        "op": 6,
        "d": {
            "token": f"QQBot {token}",
            "session_id": session_id,
            "seq": sequence,
        },
    }


def heartbeat_payload(sequence: int | None) -> dict[str, Any]:
    return {"op": 1, "d": sequence}
