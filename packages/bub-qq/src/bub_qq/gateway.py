from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .openapi import QQOpenAPI


@dataclass(frozen=True)
class QQSessionStartLimit:
    total: int
    remaining: int
    reset_after: int
    max_concurrency: int


@dataclass(frozen=True)
class QQGatewayInfo:
    url: str
    shards: int | None = None
    session_start_limit: QQSessionStartLimit | None = None

    @property
    def max_concurrency(self) -> int | None:
        if self.session_start_limit is None:
            return None
        return self.session_start_limit.max_concurrency


async def get_gateway(openapi: QQOpenAPI) -> QQGatewayInfo:
    payload = await openapi.get("/gateway")
    return QQGatewayInfo(url=str(payload["url"]))


async def get_shard_gateway(openapi: QQOpenAPI) -> QQGatewayInfo:
    payload = await openapi.get("/gateway/bot")
    return QQGatewayInfo(
        url=str(payload["url"]),
        shards=int(payload["shards"]) if payload.get("shards") is not None else None,
        session_start_limit=_parse_session_start_limit(payload.get("session_start_limit")),
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


def _parse_session_start_limit(payload: Any) -> QQSessionStartLimit | None:
    if not isinstance(payload, dict):
        return None
    required_fields = ("total", "remaining", "reset_after", "max_concurrency")
    if any(payload.get(field) is None for field in required_fields):
        return None
    return QQSessionStartLimit(
        total=int(payload["total"]),
        remaining=int(payload["remaining"]),
        reset_after=int(payload["reset_after"]),
        max_concurrency=int(payload["max_concurrency"]),
    )
