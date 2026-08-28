from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

import aiohttp
from loguru import logger

from ..config import QQConfig
from ..protocol.auth import QQAuthError
from ..protocol.errors import QQOpenAPIError
from ..protocol.openapi import QQOpenAPI
from .info import QQGatewayInfo
from .info import get_gateway
from .info import get_shard_gateway
from .info import heartbeat_payload
from .info import identify_payload
from .info import resume_payload
from .ws_errors import FATAL_WEBSOCKET_CODES
from .ws_errors import QQWebSocketFatalError
from .ws_errors import raise_for_close_code

WebSocketCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

# How long to wait for the server close frame after op 7/9, so the close code
# (e.g. 4013/4014 intent errors) can be surfaced in logs instead of being lost.
_CLOSE_DRAIN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class _ShardSpec:
    index: int
    total: int
    url: str
    use_shard: bool
    max_concurrency: int | None = None

    @property
    def shard(self) -> tuple[int, int] | None:
        if not self.use_shard:
            return None
        return (self.index, self.total)

    @property
    def label(self) -> str:
        if not self.use_shard:
            return "single"
        return f"{self.index}/{self.total}"


@dataclass
class _ShardState:
    sequence: int | None = None
    session_id: str | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    session_established: bool = False


class QQWebSocketClient:
    """QQ gateway websocket receiver."""

    def __init__(
        self,
        config: QQConfig,
        openapi: QQOpenAPI,
        on_payload: WebSocketCallback,
        *,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._openapi = openapi
        self._on_payload = on_payload
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._shard_states: dict[int, _ShardState] = {}
        self._identify_lock: asyncio.Lock | None = None
        self._identify_attempts: deque[float] = deque()
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep

    async def start(self, stop_event: asyncio.Event | None = None) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = stop_event or asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._shard_states.clear()
        self._identify_attempts.clear()
        logger.info("qq.websocket.stopped")

    async def _run(self) -> None:
        consecutive_failures = 0
        while not self._should_stop():
            try:
                specs = await self._resolve_shard_specs()
            except asyncio.CancelledError:
                raise
            except (QQAuthError, QQOpenAPIError) as exc:
                if _is_permanent_connect_error(exc):
                    logger.error("qq.websocket.permanent_error error={}", exc)
                    if self._stop_event is not None:
                        self._stop_event.set()
                    break
                logger.warning("qq.websocket.error error={}", exc)
            except Exception as exc:
                logger.warning("qq.websocket.error error={}", exc)
            else:
                self._reset_shard_states(specs)
                await self._run_workers(specs)
                break

            if self._should_stop():
                break
            consecutive_failures += 1
            await self._sleep(self._reconnect_delay(consecutive_failures))

    async def _run_workers(self, specs: list[_ShardSpec]) -> None:
        try:
            async with asyncio.TaskGroup() as task_group:
                task_group.create_task(self._watch_stop_event())
                for spec in specs:
                    task_group.create_task(self._run_shard(spec))
        except* QQWebSocketStopRequested:
            pass
        except* QQWebSocketFatalError:
            if self._stop_event is not None:
                self._stop_event.set()
        except* QQWebSocketRetryExhausted:
            if self._stop_event is not None:
                self._stop_event.set()
        except* QQAuthError:
            if self._stop_event is not None:
                self._stop_event.set()
        except* QQOpenAPIError:
            if self._stop_event is not None:
                self._stop_event.set()

    async def _watch_stop_event(self) -> None:
        if self._stop_event is None:
            return
        await self._stop_event.wait()
        raise QQWebSocketStopRequested()

    async def _resolve_shard_specs(self) -> list[_ShardSpec]:
        gateway = (
            await get_shard_gateway(self._openapi)
            if self._config.websocket_use_shard_gateway
            else await get_gateway(self._openapi)
        )
        return self._build_shard_specs(gateway)

    def _build_shard_specs(self, gateway: QQGatewayInfo) -> list[_ShardSpec]:
        if not self._config.websocket_use_shard_gateway:
            return [_ShardSpec(index=0, total=1, url=gateway.url, use_shard=False)]

        total = gateway.shards or 1
        if total < 1:
            total = 1
        return [
            _ShardSpec(
                index=index,
                total=total,
                url=gateway.url,
                use_shard=True,
                max_concurrency=gateway.max_concurrency,
            )
            for index in range(total)
        ]

    def _reset_shard_states(self, specs: list[_ShardSpec]) -> None:
        self._shard_states = {spec.index: _ShardState() for spec in specs}
        self._identify_attempts.clear()

    async def _run_shard(self, spec: _ShardSpec) -> None:
        consecutive_failures = 0
        identify_rejections = 0
        while not self._should_stop():
            state = self._shard_states[spec.index]
            state.session_established = False
            invalid_session = False
            try:
                await self._connect_once(spec)
            except asyncio.CancelledError:
                raise
            except QQWebSocketFatalError as exc:
                logger.error(
                    "qq.websocket.fatal shard={} code={} message={}",
                    spec.label,
                    exc.code,
                    exc,
                )
                raise
            except QQWebSocketReconnectRequested as exc:
                logger.warning(
                    "qq.websocket.reconnect_requested shard={} close_code={} reason=server_requested_reconnect",
                    spec.label,
                    exc.close_code,
                )
            except QQWebSocketInvalidSession as exc:
                invalid_session = True
                logger.warning(
                    "qq.websocket.invalid_session shard={} close_code={} detail={} action=identify_from_scratch",
                    spec.label,
                    exc.close_code,
                    exc.detail,
                )
                state.session_id = None
                state.sequence = None
            except (QQAuthError, QQOpenAPIError) as exc:
                if _is_permanent_connect_error(exc):
                    logger.error(
                        "qq.websocket.permanent_error shard={} error={}",
                        spec.label,
                        exc,
                    )
                    raise
                logger.warning("qq.websocket.error shard={} error={}", spec.label, exc)
            except Exception as exc:
                logger.warning("qq.websocket.error shard={} error={}", spec.label, exc)

            if state.session_established:
                consecutive_failures = 0
                identify_rejections = 0
            else:
                consecutive_failures += 1
                if invalid_session:
                    identify_rejections += 1
                    self._raise_if_rejections_exhausted(spec, identify_rejections)

            if self._should_stop():
                break
            delay = self._reconnect_delay(consecutive_failures)
            logger.info(
                "qq.websocket.reconnect_wait shard={} consecutive_failures={} delay_seconds={}",
                spec.label,
                consecutive_failures,
                delay,
            )
            await self._sleep(delay)

    def _raise_if_rejections_exhausted(self, spec: _ShardSpec, rejections: int) -> None:
        limit = self._config.websocket_max_identify_rejections
        if limit < 1 or rejections < limit:
            return
        message = (
            f"identify rejected {rejections} consecutive times without a successful"
            " session; check intents, IP whitelist, and bot status on the QQ open"
            " platform"
        )
        logger.error(
            "qq.websocket.retry_exhausted shard={} rejections={} message={}",
            spec.label,
            rejections,
            message,
        )
        raise QQWebSocketRetryExhausted(message)

    def _reconnect_delay(self, consecutive_failures: int) -> float:
        base = self._config.websocket_reconnect_delay_seconds
        max_delay = self._config.websocket_reconnect_max_delay_seconds
        if consecutive_failures <= 1:
            return min(base, max_delay)
        exponent = min(consecutive_failures - 1, 10)
        return min(base * (2.0**exponent), max_delay)

    async def _connect_once(self, spec: _ShardSpec) -> None:
        state = self._shard_states[spec.index]
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=self._config.timeout_seconds
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(spec.url, heartbeat=None) as ws:
                logger.info(
                    "qq.websocket.connected shard={} url={}", spec.label, spec.url
                )
                heartbeat_interval = await self._await_hello(ws)
                await self._identify_or_resume(ws, spec, state)
                state.heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(ws, state, heartbeat_interval)
                )
                server_signal: (
                    QQWebSocketReconnectRequested | QQWebSocketInvalidSession | None
                ) = None
                try:
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_text_frame(ws, spec, state, message.data)
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError(
                                f"qq websocket error frame: {ws.exception()}"
                            )
                        elif message.type in {
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                        }:
                            break
                except (
                    QQWebSocketReconnectRequested,
                    QQWebSocketInvalidSession,
                ) as exc:
                    server_signal = exc
                finally:
                    if state.heartbeat_task is not None:
                        state.heartbeat_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await state.heartbeat_task
                        state.heartbeat_task = None
                if server_signal is not None:
                    server_signal.close_code = await self._drain_close_code(ws)
                    if server_signal.close_code in FATAL_WEBSOCKET_CODES:
                        raise_for_close_code(server_signal.close_code)
                    raise server_signal
                raise_for_close_code(ws.close_code)

    async def _drain_close_code(
        self, ws: aiohttp.ClientWebSocketResponse
    ) -> int | None:
        """Wait briefly for the server close frame so its close code is observable."""
        try:
            async with asyncio.timeout(_CLOSE_DRAIN_TIMEOUT_SECONDS):
                while not ws.closed:
                    message = await ws.receive()
                    if message.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        break
        except Exception:
            pass
        with contextlib.suppress(Exception):
            await ws.close()
        return ws.close_code

    async def _await_hello(self, ws: aiohttp.ClientWebSocketResponse) -> float:
        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError(
                    f"qq websocket expected hello text frame, got {message.type}"
                )
            payload = _parse_payload(message.data)
            op = payload.get("op")
            if op != 10:
                continue
            data = payload.get("d")
            if not isinstance(data, dict) or "heartbeat_interval" not in data:
                raise RuntimeError("qq websocket hello missing heartbeat_interval")
            interval_ms = float(data["heartbeat_interval"])
            return interval_ms / 1000.0

    async def _identify_or_resume(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        spec: _ShardSpec,
        state: _ShardState,
    ) -> None:
        token = await self._openapi.get_access_token()
        if state.session_id and state.sequence is not None:
            logger.info(
                "qq.websocket.resume_attempt shard={} session_id={} seq={}",
                spec.label,
                state.session_id,
                state.sequence,
            )
            await ws.send_json(
                resume_payload(
                    token=token,
                    session_id=state.session_id,
                    sequence=state.sequence,
                )
            )
            return

        await self._await_identify_slot(spec.max_concurrency)
        await ws.send_json(
            identify_payload(
                token=token,
                intents=self._config.websocket_intents,
                shard=spec.shard,
            )
        )

    async def _await_identify_slot(self, max_concurrency: int | None) -> None:
        if max_concurrency is None or max_concurrency < 1:
            return
        while True:
            lock = self._get_identify_lock()
            async with lock:
                now = self._monotonic()
                while (
                    self._identify_attempts and now - self._identify_attempts[0] >= 5.0
                ):
                    self._identify_attempts.popleft()
                if len(self._identify_attempts) < max_concurrency:
                    self._identify_attempts.append(now)
                    return
                wait_seconds = max(0.0, 5.0 - (now - self._identify_attempts[0]))
            await self._sleep(wait_seconds)

    def _get_identify_lock(self) -> asyncio.Lock:
        if self._identify_lock is None:
            self._identify_lock = asyncio.Lock()
        return self._identify_lock

    async def _heartbeat_loop(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        state: _ShardState,
        interval_seconds: float,
    ) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await ws.send_json(heartbeat_payload(state.sequence))

    async def _handle_text_frame(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        spec: _ShardSpec,
        state: _ShardState,
        text: str,
    ) -> None:
        payload = _parse_payload(text)
        op = payload.get("op")
        if op == 11:
            return
        if op == 7:
            raise QQWebSocketReconnectRequested()
        if op == 9:
            raise QQWebSocketInvalidSession(payload.get("d"))
        if op == 1:
            await ws.send_json(heartbeat_payload(state.sequence))
            return
        await self._dispatch_if_needed(spec, state, payload)

    async def _dispatch_if_needed(
        self,
        spec: _ShardSpec,
        state: _ShardState,
        payload: dict[str, Any],
    ) -> None:
        if payload.get("s") is not None:
            try:
                state.sequence = int(payload["s"])
            except (TypeError, ValueError):
                state.sequence = state.sequence
        if payload.get("op") == 0:
            event_type = payload.get("t")
            if event_type == "READY":
                state.session_established = True
                data = payload.get("d")
                if isinstance(data, dict):
                    session_id = data.get("session_id")
                    if isinstance(session_id, str) and session_id.strip():
                        state.session_id = session_id.strip()
            elif event_type == "RESUMED":
                state.session_established = True
                if state.session_id:
                    logger.info(
                        "qq.websocket.resume_succeeded shard={} session_id={}",
                        spec.label,
                        state.session_id,
                    )
            await self._on_payload(payload)

    def _should_stop(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()


def _parse_payload(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise RuntimeError("qq websocket payload must be a JSON object")
    return payload


class QQWebSocketReconnectRequested(RuntimeError):
    def __init__(self) -> None:
        super().__init__("qq websocket reconnect requested by server")
        self.close_code: int | None = None


class QQWebSocketInvalidSession(RuntimeError):
    def __init__(self, detail: Any = None) -> None:
        super().__init__("qq websocket invalid session")
        self.detail = detail
        self.close_code: int | None = None


class QQWebSocketRetryExhausted(RuntimeError):
    """Raised after too many consecutive identify rejections; stops the client."""


class QQWebSocketStopRequested(RuntimeError):
    def __init__(self) -> None:
        super().__init__("qq websocket stop requested")


def _is_permanent_connect_error(exc: QQAuthError | QQOpenAPIError) -> bool:
    if isinstance(exc, QQAuthError):
        return True
    if exc.known is not None:
        return not exc.known.retryable
    return 400 <= exc.status_code < 500 and exc.status_code != 429
