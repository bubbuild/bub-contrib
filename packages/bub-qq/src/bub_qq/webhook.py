from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Coroutine
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from loguru import logger

from .config import QQConfig
from .signature import sign_validation_payload
from .signature import verify_request_signature

WebhookCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class QQWebhookServer:
    """Embedded HTTP webhook receiver for QQ callback events."""

    def __init__(
        self,
        config: QQConfig,
        on_payload: WebhookCallback,
    ) -> None:
        self._config = config
        self._on_payload = on_payload
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    async def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        handler = self._build_handler()
        self._server = ThreadingHTTPServer(
            (self._config.webhook_host, self._config.webhook_port),
            handler,
        )
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="qq-webhook-server",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "qq.webhook.start host={} port={} path={}",
            self._config.webhook_host,
            self._config.webhook_port,
            self._config.webhook_path,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("qq.webhook.stopped")

    def _build_handler(self) -> type[BaseHTTPRequestHandler]:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                parent._handle_post(self)

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("qq.webhook.http " + format, *args)

        return Handler

    def _handle_post(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path != self._config.webhook_path:
            self._write_json(handler, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            body = self._read_body(handler)
        except ValueError as exc:
            self._write_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if self._config.verify_signature and not self._is_signature_valid(handler, body):
            self._write_json(handler, HTTPStatus.UNAUTHORIZED, {"error": "invalid signature"})
            return

        try:
            payload = self._parse_json(body)
        except ValueError as exc:
            self._write_json(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        op = payload.get("op")
        if op == 13:
            self._handle_validation(handler, payload)
            return

        if self._loop is None:
            self._write_json(handler, HTTPStatus.SERVICE_UNAVAILABLE, {"error": "loop not ready"})
            return

        self._schedule_payload(payload)
        self._write_json(handler, HTTPStatus.OK, {"op": 12})

    def _handle_validation(
        self,
        handler: BaseHTTPRequestHandler,
        payload: dict[str, Any],
    ) -> None:
        data = payload.get("d")
        if not isinstance(data, dict):
            self._write_json(handler, HTTPStatus.BAD_REQUEST, {"error": "payload.d must be an object"})
            return

        plain_token = str(data.get("plain_token") or "")
        event_ts = str(data.get("event_ts") or "")
        if not plain_token or not event_ts:
            self._write_json(handler, HTTPStatus.BAD_REQUEST, {"error": "validation payload incomplete"})
            return

        signature = sign_validation_payload(
            secret=self._config.secret,
            event_ts=event_ts,
            plain_token=plain_token,
        )
        self._write_json(
            handler,
            HTTPStatus.OK,
            {"plain_token": plain_token, "signature": signature},
        )

    def _read_body(self, handler: BaseHTTPRequestHandler) -> bytes:
        content_length = int(handler.headers.get("Content-Length", "0"))
        return handler.rfile.read(content_length)

    def _parse_json(self, body: bytes) -> dict[str, Any]:
        payload = json.loads(body.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a JSON object")
        return payload

    def _is_signature_valid(self, handler: BaseHTTPRequestHandler, body: bytes) -> bool:
        signature = handler.headers.get("X-Signature-Ed25519", "")
        timestamp = handler.headers.get("X-Signature-Timestamp", "")
        if not signature or not timestamp:
            logger.warning("qq.webhook.signature_missing")
            return False
        return verify_request_signature(
            secret=self._config.secret,
            timestamp=timestamp,
            body=body,
            signature_hex=signature,
        )

    def _schedule_payload(self, payload: dict[str, Any]) -> None:
        if self._loop is None:
            raise RuntimeError("qq webhook loop not ready")

        future = asyncio.run_coroutine_threadsafe(self._on_payload(payload), self._loop)
        future.add_done_callback(
            lambda task: self._log_callback_result(
                task,
                op=payload.get("op"),
                event_type=payload.get("t"),
            )
        )

    def _log_callback_result(self, future: Any, *, op: Any, event_type: Any) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("qq.webhook.callback_failed op={} t={}", op, event_type)

    def _write_json(
        self,
        handler: BaseHTTPRequestHandler,
        status: HTTPStatus,
        payload: dict[str, Any],
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
