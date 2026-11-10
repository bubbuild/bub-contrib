from __future__ import annotations

import asyncio
import platform

import pytest

from bub_qq.config import QQConfig
from bub_qq.gateway.websocket import QQWebSocketClient
from bub_qq.gateway.websocket import QQWebSocketInvalidSession
from bub_qq.gateway.websocket import QQWebSocketRetryExhausted
from bub_qq.gateway.websocket import _ShardSpec
from bub_qq.gateway.websocket import _ShardState


class OpenAPIStub:
    def __init__(self, payloads: dict[str, dict[str, object]] | None = None) -> None:
        self.payloads = payloads or {}
        self.calls: list[str] = []

    async def get(
        self, path: str, *, params: dict[str, object] | None = None
    ) -> dict[str, object]:
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
        client = QQWebSocketClient(
            QQConfig(receive_mode="websocket"),
            openapi,
            _on_payload,
        )  # type: ignore[arg-type]

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
            QQConfig(receive_mode="websocket", websocket_use_shard_gateway=True),
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
            QQConfig(receive_mode="websocket", websocket_use_shard_gateway=True),
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
                        "$os": platform.system().lower() or "unknown",
                        "$browser": "bub-qq",
                        "$device": "bub-qq",
                    },
                    "shard": [2, 5],
                },
            }
        ]

    asyncio.run(_run())


def test_reconnect_delay_grows_exponentially_and_caps() -> None:
    client = QQWebSocketClient(
        QQConfig(receive_mode="websocket"),
        OpenAPIStub(),
        _on_payload,
    )  # type: ignore[arg-type]

    assert client._reconnect_delay(0) == 5.0
    assert client._reconnect_delay(1) == 5.0
    assert client._reconnect_delay(2) == 10.0
    assert client._reconnect_delay(3) == 20.0
    assert client._reconnect_delay(100) == 300.0


def test_run_shard_stops_after_max_consecutive_identify_rejections() -> None:
    async def _run() -> None:
        clock = ManualClock()
        client = QQWebSocketClient(
            QQConfig(receive_mode="websocket", websocket_max_identify_rejections=3),
            OpenAPIStub(),
            _on_payload,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )  # type: ignore[arg-type]
        spec = _ShardSpec(
            index=0,
            total=1,
            url="wss://api.sgroup.qq.com/websocket/",
            use_shard=False,
        )
        client._shard_states = {0: _ShardState()}

        async def _always_rejected(spec: _ShardSpec) -> None:
            del spec
            raise QQWebSocketInvalidSession({"resumable": False})

        client._connect_once = _always_rejected  # type: ignore[method-assign]

        with pytest.raises(QQWebSocketRetryExhausted):
            await client._run_shard(spec)

        assert clock.sleeps == [5.0, 10.0]
        assert client._shard_states[0].session_id is None
        assert client._shard_states[0].sequence is None

    asyncio.run(_run())


def test_run_shard_resets_rejection_count_after_established_session() -> None:
    async def _run() -> None:
        clock = ManualClock()
        client = QQWebSocketClient(
            QQConfig(receive_mode="websocket", websocket_max_identify_rejections=2),
            OpenAPIStub(),
            _on_payload,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )  # type: ignore[arg-type]
        spec = _ShardSpec(
            index=0,
            total=1,
            url="wss://api.sgroup.qq.com/websocket/",
            use_shard=False,
        )
        client._shard_states = {0: _ShardState()}
        attempts = 0

        async def _reject_except_second_attempt(spec: _ShardSpec) -> None:
            del spec
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                # Session came up, then the connection dropped normally.
                client._shard_states[0].session_established = True
                return
            raise QQWebSocketInvalidSession(None)

        client._connect_once = _reject_except_second_attempt  # type: ignore[method-assign]

        with pytest.raises(QQWebSocketRetryExhausted):
            await client._run_shard(spec)

        # With limit=2: rejection at attempt 1 (count 1), success at attempt 2
        # resets the counter, rejections at attempts 3 and 4 reach the limit.
        # Without the reset the limit would already trip at attempt 3.
        assert attempts == 4

    asyncio.run(_run())


def test_identify_rate_limit_waits_for_next_window() -> None:
    async def _run() -> None:
        clock = ManualClock()
        client = QQWebSocketClient(
            QQConfig(receive_mode="websocket"),
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


def test_fatal_error_stops_qq_but_not_the_gateway() -> None:
    from bub_qq.protocol.auth import QQAuthError

    async def _run() -> None:
        client = QQWebSocketClient(QQConfig.model_construct(), openapi=None, on_payload=None)

        async def broken_login() -> list:
            raise QQAuthError("qq token request failed: http=401")

        client._resolve_shard_specs = broken_login
        gateway_stop = asyncio.Event()
        await client.start(gateway_stop)
        await asyncio.wait_for(client._task, timeout=2)

        assert client._should_stop()
        assert not gateway_stop.is_set()  # other channels keep running

    asyncio.run(_run())


def test_gateway_stop_event_still_stops_the_client() -> None:
    async def _run() -> None:
        client = QQWebSocketClient(QQConfig.model_construct(), openapi=None, on_payload=None)
        gateway_stop = asyncio.Event()
        client._external_stop = gateway_stop
        client._stop_event = asyncio.Event()
        watcher = asyncio.create_task(client._watch_stop_event())
        await asyncio.sleep(0)
        gateway_stop.set()
        with pytest.raises(Exception, match="stop requested"):
            await asyncio.wait_for(watcher, timeout=2)

    asyncio.run(_run())
