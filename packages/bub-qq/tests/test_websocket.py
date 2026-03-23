from __future__ import annotations

import asyncio

from bub_qq.config import QQConfig
from bub_qq.websocket import QQWebSocketClient
from bub_qq.websocket import _ShardSpec
from bub_qq.websocket import _ShardState


class OpenAPIStub:
    def __init__(self, payloads: dict[str, dict[str, object]] | None = None) -> None:
        self.payloads = payloads or {}
        self.calls: list[str] = []

    async def get(self, path: str, *, params: dict[str, object] | None = None) -> dict[str, object]:
        del params
        self.calls.append(path)
        return self.payloads[path]

    async def get_access_token(self) -> str:
        return "token"


class WebSocketStub:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.payloads.append(payload)


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def _on_payload(payload: dict[str, object]) -> None:
    del payload


def test_resolve_shard_specs_uses_gateway_endpoint_when_unsharded() -> None:
    async def _run() -> None:
        openapi = OpenAPIStub(
            {
                "/gateway": {
                    "url": "wss://api.sgroup.qq.com/websocket/",
                }
            }
        )
        client = QQWebSocketClient(QQConfig(), openapi, _on_payload)  # type: ignore[arg-type]

        specs = await client._resolve_shard_specs()

        assert openapi.calls == ["/gateway"]
        assert len(specs) == 1
        assert specs[0].use_shard is False
        assert specs[0].shard is None

    asyncio.run(_run())


def test_resolve_shard_specs_creates_one_worker_per_recommended_shard() -> None:
    async def _run() -> None:
        openapi = OpenAPIStub(
            {
                "/gateway/bot": {
                    "url": "wss://api.sgroup.qq.com/websocket/",
                    "shards": 3,
                    "session_start_limit": {
                        "total": 1000,
                        "remaining": 999,
                        "reset_after": 14400000,
                        "max_concurrency": 2,
                    },
                }
            }
        )
        client = QQWebSocketClient(
            QQConfig(websocket_use_shard_gateway=True),
            openapi,
            _on_payload,
        )  # type: ignore[arg-type]

        specs = await client._resolve_shard_specs()

        assert openapi.calls == ["/gateway/bot"]
        assert [spec.shard for spec in specs] == [(0, 3), (1, 3), (2, 3)]
        assert all(spec.max_concurrency == 2 for spec in specs)

    asyncio.run(_run())


def test_identify_uses_current_shard_index_and_total() -> None:
    async def _run() -> None:
        openapi = OpenAPIStub()
        client = QQWebSocketClient(
            QQConfig(websocket_use_shard_gateway=True),
            openapi,
            _on_payload,
        )  # type: ignore[arg-type]
        ws = WebSocketStub()

        await client._identify_or_resume(
            ws,  # type: ignore[arg-type]
            _ShardSpec(
                index=2,
                total=5,
                url="wss://api.sgroup.qq.com/websocket/",
                use_shard=True,
                max_concurrency=None,
            ),
            _ShardState(),
        )

        assert ws.payloads == [
            {
                "op": 2,
                "d": {
                    "token": "QQBot token",
                    "intents": 1 << 25,
                    "properties": {
                        "$os": "macos",
                        "$browser": "bub-qq",
                        "$device": "bub-qq",
                    },
                    "shard": [2, 5],
                },
            }
        ]

    asyncio.run(_run())


def test_identify_rate_limit_waits_for_next_window() -> None:
    async def _run() -> None:
        clock = ManualClock()
        client = QQWebSocketClient(
            QQConfig(),
            OpenAPIStub(),
            _on_payload,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )  # type: ignore[arg-type]

        await client._await_identify_slot(1)
        await client._await_identify_slot(1)

        assert clock.sleeps == [5.0]
        assert clock.now == 5.0

    asyncio.run(_run())
