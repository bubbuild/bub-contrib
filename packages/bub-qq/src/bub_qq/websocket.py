from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp
from loguru import logger

from .config import QQConfig
from .gateway import get_gateway
from .gateway import get_shard_gateway
from .gateway import heartbeat_payload
from .gateway import identify_payload
from .gateway import resume_payload
from .openapi import QQOpenAPI
from .ws_errors import QQWebSocketFatalError
from .ws_errors import raise_for_close_code

WebSocketCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class QQWebSocketClient:
    """QQ gateway websocket receiver."""

    def __init__(
        self,
        config: QQConfig,
        openapi: QQOpenAPI,
        on_payload: WebSocketCallback,
    ) -> None:
        self._config = config
        self._openapi = openapi
        self._on_payload = on_payload
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._sequence: int | None = None
        self._session_id: str | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def start(self, stop_event: asyncio.Event | None = None) -> None:
        if self._task is not None:
            return
        self._stop_event = stop_event or asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("qq.websocket.stopped")

    async def _run(self) -> None:
        while not (self._stop_event and self._stop_event.is_set()):
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except QQWebSocketFatalError as exc:
                logger.error("qq.websocket.fatal code={} message={}", exc.code, exc)
                if self._stop_event is not None:
                    self._stop_event.set()
                break
            except QQWebSocketReconnectRequested:
                logger.warning(
                    "qq.websocket.reconnect_requested reason=server_requested_reconnect delay_seconds={}",
                    self._config.websocket_reconnect_delay_seconds,
                )
            except QQWebSocketInvalidSession:
                logger.warning("qq.websocket.invalid_session action=identify_from_scratch")
                self._session_id = None
                self._sequence = None
            except Exception as exc:
                logger.warning("qq.websocket.error error={}", exc)
            if self._stop_event and self._stop_event.is_set():
                break
            await asyncio.sleep(self._config.websocket_reconnect_delay_seconds)

    async def _connect_once(self) -> None:
        gateway = (
            await get_shard_gateway(self._openapi)
            if self._config.websocket_use_shard_gateway
            else await get_gateway(self._openapi)
        )
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self._config.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(gateway.url, heartbeat=None) as ws:
                logger.info("qq.websocket.connected url={}", gateway.url)
                heartbeat_interval = await self._await_hello(ws)
                await self._identify_or_resume(ws, gateway)
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(ws, heartbeat_interval)
                )
                try:
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_text_frame(ws, message.data)
                        elif message.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError(f"qq websocket error frame: {ws.exception()}")
                        elif message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}:
                            break
                finally:
                    if self._heartbeat_task is not None:
                        self._heartbeat_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await self._heartbeat_task
                        self._heartbeat_task = None
                raise_for_close_code(ws.close_code)

    async def _await_hello(self, ws: aiohttp.ClientWebSocketResponse) -> float:
        while True:
            message = await ws.receive()
            if message.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError(f"qq websocket expected hello text frame, got {message.type}")
            payload = _parse_payload(message.data)
            op = payload.get("op")
            if op != 10:
                await self._dispatch_if_needed(payload)
                continue
            data = payload.get("d")
            if not isinstance(data, dict) or "heartbeat_interval" not in data:
                raise RuntimeError("qq websocket hello missing heartbeat_interval")
            interval_ms = float(data["heartbeat_interval"])
            return interval_ms / 1000.0

    async def _identify_or_resume(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        gateway: Any,
    ) -> None:
        token = await self._openapi.get_access_token()
        if self._session_id and self._sequence is not None:
            logger.info(
                "qq.websocket.resume_attempt session_id={} seq={}",
                self._session_id,
                self._sequence,
            )
            await ws.send_json(
                resume_payload(
                    token=token,
                    session_id=self._session_id,
                    sequence=self._sequence,
                )
            )
            return

        shard: tuple[int, int] | None = None
        if self._config.websocket_use_shard_gateway and getattr(gateway, "shards", None):
            shard = (0, int(gateway.shards))
        await ws.send_json(
            identify_payload(
                token=token,
                intents=self._config.websocket_intents,
                shard=shard,
            )
        )

    async def _heartbeat_loop(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        interval_seconds: float,
    ) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await ws.send_json(heartbeat_payload(self._sequence))

    async def _handle_text_frame(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        text: str,
    ) -> None:
        payload = _parse_payload(text)
        op = payload.get("op")
        if op == 11:
            return
        if op == 7:
            raise QQWebSocketReconnectRequested()
        if op == 9:
            raise QQWebSocketInvalidSession()
        if op == 1:
            await ws.send_json(heartbeat_payload(self._sequence))
            return
        await self._dispatch_if_needed(payload)

    async def _dispatch_if_needed(self, payload: dict[str, Any]) -> None:
        if payload.get("s") is not None:
            try:
                self._sequence = int(payload["s"])
            except (TypeError, ValueError):
                self._sequence = self._sequence
        if payload.get("op") == 0:
            event_type = payload.get("t")
            if event_type == "READY":
                data = payload.get("d")
                if isinstance(data, dict):
                    session_id = data.get("session_id")
                    if isinstance(session_id, str) and session_id.strip():
                        self._session_id = session_id.strip()
            elif event_type == "RESUMED" and self._session_id:
                logger.info("qq.websocket.resume_succeeded session_id={}", self._session_id)
        if payload.get("op") == 0:
            await self._on_payload(payload)


def _parse_payload(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise RuntimeError("qq websocket payload must be a JSON object")
    return payload


class QQWebSocketReconnectRequested(RuntimeError):
    def __init__(self) -> None:
        super().__init__("qq websocket reconnect requested by server")


class QQWebSocketInvalidSession(RuntimeError):
    def __init__(self) -> None:
        super().__init__("qq websocket invalid session")
