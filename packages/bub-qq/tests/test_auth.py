from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import Protocol

from bub_qq.auth import QQTokenProvider
from bub_qq.config import QQConfig
from bub_qq.openapi import QQOpenAPI
from bub_qq.openapi_errors import lookup_known_error


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeResponse:
    def __init__(
        self,
        *,
        status: int,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        reason: str = "OK",
    ) -> None:
        self.status = status
        self.payload = payload
        self.headers = headers or {}
        self.reason = reason


class OpenAPIRequest(Protocol):
    method: str
    url: str
    params: dict[str, object] | None
    json: dict[str, object] | None
    headers: dict[str, str]


class _OpenAPIRequest:
    def __init__(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, object] | None,
        json: dict[str, object] | None,
        headers: dict[str, str],
    ) -> None:
        self.method = method
        self.url = url
        self.params = params
        self.json = json
        self.headers = headers


class FakeTokenClient:
    def __init__(
        self,
        handler: Callable[[str, dict[str, object]], Awaitable[FakeResponse]],
    ) -> None:
        self._handler = handler

    async def post(self, url: str, **kwargs: object) -> dict[str, object]:
        response = await self._handler(url, kwargs)
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(
                f"qq token request failed: http={response.status} reason={response.reason}"
            )
        if not isinstance(response.payload, dict):
            raise RuntimeError(
                f"qq token response is not a JSON object: {response.payload!r}"
            )
        return response.payload


class FakeOpenAPIClient:
    def __init__(
        self, handler: Callable[[OpenAPIRequest], Awaitable[FakeResponse]]
    ) -> None:
        self._handler = handler

    async def request(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, object] | None,
        json: dict[str, object] | None,
        headers: dict[str, str],
    ) -> FakeResponse:
        return await self._handler(
            _OpenAPIRequest(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
            )
        )


def test_token_provider_caches_until_refresh_boundary() -> None:
    async def _run() -> None:
        calls = {"count": 0}

        async def handler(url: str, kwargs: dict[str, object]) -> FakeResponse:
            del url, kwargs
            calls["count"] += 1
            return FakeResponse(
                status=200,
                payload={
                    "access_token": f"token-{calls['count']}",
                    "expires_in": 120,
                },
            )

        clock = Clock()
        provider = QQTokenProvider(
            QQConfig(
                appid="app",
                secret="secret",
                receive_mode="webhook",
                token_refresh_skew_seconds=60,
            ),
            client=FakeTokenClient(handler),
            clock=clock,
        )

        assert await provider.get_token() == "token-1"
        assert await provider.get_token() == "token-1"

        clock.now = 59
        assert await provider.get_token() == "token-1"

        clock.now = 60
        assert await provider.get_token() == "token-2"

    asyncio.run(_run())


def test_openapi_adds_authorization_header() -> None:
    async def _run() -> None:
        captured: dict[str, str] = {}

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            captured["authorization"] = request.headers["Authorization"]
            captured["content_type"] = request.headers["Content-Type"]
            return FakeResponse(status=200, payload={"ok": True})

        async def token_handler(url: str, kwargs: dict[str, object]) -> FakeResponse:
            del url, kwargs
            return FakeResponse(
                status=200,
                payload={"access_token": "abc", "expires_in": 7200},
            )

        provider = QQTokenProvider(
            QQConfig(appid="app", secret="secret", receive_mode="webhook"),
            client=FakeTokenClient(token_handler),
        )
        openapi = QQOpenAPI(
            QQConfig(receive_mode="webhook"),
            provider,
            client=FakeOpenAPIClient(openapi_handler),
        )

        payload = await openapi.post("/test", json_body={"ping": "pong"})

        assert payload == {"ok": True}
        assert captured["authorization"] == "QQBot abc"
        assert captured["content_type"] == "application/json"

    asyncio.run(_run())


def test_openapi_posts_c2c_text_message() -> None:
    async def _run() -> None:
        captured: dict[str, object] = {}

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            captured["path"] = request.url
            captured["json"] = request.json
            return FakeResponse(status=200, payload={"id": "reply-1", "timestamp": 123})

        async def token_handler(url: str, kwargs: dict[str, object]) -> FakeResponse:
            del url, kwargs
            return FakeResponse(
                status=200,
                payload={"access_token": "abc", "expires_in": 7200},
            )

        provider = QQTokenProvider(
            QQConfig(appid="app", secret="secret", receive_mode="webhook"),
            client=FakeTokenClient(token_handler),
        )
        openapi = QQOpenAPI(
            QQConfig(receive_mode="webhook"),
            provider,
            client=FakeOpenAPIClient(openapi_handler),
        )

        payload = await openapi.post_c2c_text_message(
            openid="user-openid",
            content="hello",
            msg_id="message-1",
            msg_seq=2,
        )

        assert payload["id"] == "reply-1"
        assert captured["path"] == "/v2/users/user-openid/messages"
        assert captured["json"] == {
            "content": "hello",
            "msg_type": 0,
            "msg_id": "message-1",
            "msg_seq": 2,
        }

    asyncio.run(_run())


def test_openapi_error_exposes_trace_id_and_business_code() -> None:
    async def _run() -> None:
        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            del request
            return FakeResponse(
                status=429,
                headers={"X-Tps-trace-ID": "trace-123"},
                payload={"code": 22009, "message": "msg limit exceed"},
                reason="Too Many Requests",
            )

        async def token_handler(url: str, kwargs: dict[str, object]) -> FakeResponse:
            del url, kwargs
            return FakeResponse(
                status=200,
                payload={"access_token": "abc", "expires_in": 7200},
            )

        provider = QQTokenProvider(
            QQConfig(appid="app", secret="secret", receive_mode="webhook"),
            client=FakeTokenClient(token_handler),
        )
        openapi = QQOpenAPI(
            QQConfig(receive_mode="webhook"),
            provider,
            client=FakeOpenAPIClient(openapi_handler),
        )

        try:
            await openapi.post("/test", json_body={"ping": "pong"})
        except Exception as exc:
            assert "trace_id=trace-123" in str(exc)
            assert "code=22009" in str(exc)
            assert "category=rate_limit" in str(exc)
            assert "msg limit exceed" in str(exc)
        else:
            raise AssertionError("expected openapi request to fail")

    asyncio.run(_run())


def test_known_openapi_error_catalog_contains_reply_expired() -> None:
    known = lookup_known_error(304027)

    assert known is not None
    assert known.name == "MSG_EXPIRE"
    assert known.category == "reply"
    assert known.retryable is False
