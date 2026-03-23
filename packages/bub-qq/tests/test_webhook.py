from __future__ import annotations

import asyncio

from bub_qq.config import QQConfig
from bub_qq.webhook import QQWebhookServer


def test_schedule_payload_runs_callback_on_loop() -> None:
    async def _run() -> None:
        received: list[dict[str, object]] = []

        async def on_payload(payload: dict[str, object]) -> None:
            received.append(payload)

        server = QQWebhookServer(QQConfig(secret="secret"), on_payload)
        server._loop = asyncio.get_running_loop()

        payload = {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"id": "event-1"}}
        server._schedule_payload(payload)
        await asyncio.sleep(0)

        assert received == [payload]

    asyncio.run(_run())


def test_schedule_payload_requires_running_loop() -> None:
    async def on_payload(payload: dict[str, object]) -> None:
        del payload

    server = QQWebhookServer(QQConfig(secret="secret"), on_payload)

    try:
        server._schedule_payload({"op": 0})
    except RuntimeError as exc:
        assert "loop not ready" in str(exc)
    else:
        raise AssertionError("expected scheduling without loop to fail")


def test_log_callback_result_swallows_handler_errors() -> None:
    loop = asyncio.new_event_loop()
    try:
        future: asyncio.Future[None] = loop.create_future()
        future.set_exception(RuntimeError("boom"))

        server = QQWebhookServer(QQConfig(secret="secret"), lambda payload: _noop(payload))
        server._log_callback_result(future, op=0, event_type="C2C_MESSAGE_CREATE")
    finally:
        loop.close()


async def _noop(payload: dict[str, object]) -> None:
    del payload
