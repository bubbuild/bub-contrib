from __future__ import annotations

import asyncio
import time

from bub_qq.config import QQConfig
from bub_qq.gateway.webhook import QQWebhookServer


def test_schedule_payload_runs_callback_on_loop() -> None:
    async def _run() -> None:
        received: list[dict[str, object]] = []

        async def on_payload(payload: dict[str, object]) -> None:
            received.append(payload)

        server = QQWebhookServer(
            QQConfig(secret="secret", receive_mode="webhook"), on_payload
        )
        server._loop = asyncio.get_running_loop()

        payload = {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"id": "event-1"}}
        server._schedule_payload(payload)
        await asyncio.sleep(0.01)

        assert received == [payload]

    asyncio.run(_run())


def test_schedule_payload_requires_running_loop() -> None:
    async def on_payload(payload: dict[str, object]) -> None:
        del payload

    server = QQWebhookServer(
        QQConfig(secret="secret", receive_mode="webhook"), on_payload
    )

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

        server = QQWebhookServer(
            QQConfig(secret="secret", receive_mode="webhook"),
            lambda payload: _noop(payload),
        )
        server._log_callback_result(future, op=0, event_type="C2C_MESSAGE_CREATE")
    finally:
        loop.close()


async def _noop(payload: dict[str, object]) -> None:
    del payload


def test_signature_timestamp_freshness_on_by_default() -> None:
    server = QQWebhookServer(
        QQConfig(secret="secret", receive_mode="webhook"),
        _noop,
    )

    assert server._is_signature_timestamp_fresh(str(time.time())) is True
    assert server._is_signature_timestamp_fresh("0") is False


def test_signature_timestamp_freshness_can_be_disabled() -> None:
    server = QQWebhookServer(
        QQConfig(
            secret="secret",
            receive_mode="webhook",
            webhook_signature_timestamp_tolerance_seconds=0,
        ),
        _noop,
    )

    assert server._is_signature_timestamp_fresh("0") is True


def test_signature_timestamp_freshness_rejects_stale_and_invalid() -> None:
    config = QQConfig(
        secret="secret",
        receive_mode="webhook",
        webhook_signature_timestamp_tolerance_seconds=300.0,
    )
    server = QQWebhookServer(config, _noop)

    assert server._is_signature_timestamp_fresh(str(time.time())) is True
    assert server._is_signature_timestamp_fresh("0") is False
    assert server._is_signature_timestamp_fresh("not-a-number") is False


def _post(port: int, body: bytes, headers: dict[str, str]) -> tuple[int, bytes]:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("POST", "/qq/webhook")
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        if body:
            conn.send(body)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_webhook_rejects_oversized_and_invalid_bodies_before_reading() -> None:
    import socket

    from bub_qq.gateway.webhook import MAX_WEBHOOK_BODY_BYTES

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    async def _run() -> None:
        server = QQWebhookServer(
            QQConfig(
                secret="secret",
                receive_mode="webhook",
                webhook_host="127.0.0.1",
                webhook_port=port,
            ),
            _noop,
        )
        await server.start()
        loop = asyncio.get_running_loop()
        try:
            status, _ = await loop.run_in_executor(
                None,
                _post,
                port,
                b"",
                {"Content-Length": str(MAX_WEBHOOK_BODY_BYTES + 1)},
            )
            assert status == 413
            status, _ = await loop.run_in_executor(
                None, _post, port, b"", {"Content-Length": "-1"}
            )
            assert status == 400
            status, _ = await loop.run_in_executor(
                None, _post, port, b"", {"Content-Length": "abc"}
            )
            assert status == 400
            # A normal-sized body still reaches signature verification.
            status, _ = await loop.run_in_executor(
                None, _post, port, b"{}", {"Content-Length": "2"}
            )
            assert status == 401
        finally:
            await server.stop()

    asyncio.run(_run())
