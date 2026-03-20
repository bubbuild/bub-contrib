from __future__ import annotations

import asyncio

import httpx

from bub_qq.auth import QQTokenProvider
from bub_qq.config import QQConfig
from bub_qq.openapi import QQOpenAPI
from bub_qq.openapi_errors import lookup_known_error


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_token_provider_caches_until_refresh_boundary() -> None:
    async def _run() -> None:
        calls = {"count": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            del request
            calls["count"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{calls['count']}",
                    "expires_in": 120,
                },
            )

        clock = Clock()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = QQTokenProvider(
            QQConfig(appid="app", secret="secret", token_refresh_skew_seconds=60),
            client=client,
            clock=clock,
        )

        assert await provider.get_token() == "token-1"
        assert await provider.get_token() == "token-1"

        clock.now = 59
        assert await provider.get_token() == "token-1"

        clock.now = 60
        assert await provider.get_token() == "token-2"

        await client.aclose()

    asyncio.run(_run())


def test_openapi_adds_authorization_header() -> None:
    async def _run() -> None:
        captured: dict[str, str] = {}

        async def openapi_handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers["Authorization"]
            captured["content_type"] = request.headers["Content-Type"]
            return httpx.Response(200, json={"ok": True})

        async def token_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={"access_token": "abc", "expires_in": 7200},
            )

        openapi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(openapi_handler),
            base_url="https://api.sgroup.qq.com",
        )
        token_client = httpx.AsyncClient(
            transport=httpx.MockTransport(token_handler),
        )
        provider = QQTokenProvider(
            QQConfig(appid="app", secret="secret"),
            client=token_client,
        )
        openapi = QQOpenAPI(QQConfig(), provider, client=openapi_client)

        payload = await openapi.post("/test", json_body={"ping": "pong"})

        assert payload == {"ok": True}
        assert captured["authorization"] == "QQBot abc"
        assert captured["content_type"] == "application/json"

        await openapi_client.aclose()
        await token_client.aclose()

    asyncio.run(_run())


def test_openapi_posts_c2c_text_message() -> None:
    async def _run() -> None:
        captured: dict[str, object] = {}

        async def openapi_handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["json"] = await request.aread()
            return httpx.Response(200, json={"id": "reply-1", "timestamp": 123})

        async def token_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={"access_token": "abc", "expires_in": 7200},
            )

        openapi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(openapi_handler),
            base_url="https://api.sgroup.qq.com",
        )
        token_client = httpx.AsyncClient(
            transport=httpx.MockTransport(token_handler),
        )
        provider = QQTokenProvider(
            QQConfig(appid="app", secret="secret"),
            client=token_client,
        )
        openapi = QQOpenAPI(QQConfig(), provider, client=openapi_client)

        payload = await openapi.post_c2c_text_message(
            openid="user-openid",
            content="hello",
            msg_id="message-1",
            msg_seq=2,
        )

        assert payload["id"] == "reply-1"
        assert captured["path"] == "/v2/users/user-openid/messages"
        assert captured["json"] == b'{"content":"hello","msg_type":0,"msg_id":"message-1","msg_seq":2}'

        await openapi_client.aclose()
        await token_client.aclose()

    asyncio.run(_run())


def test_openapi_error_exposes_trace_id_and_business_code() -> None:
    async def _run() -> None:
        async def openapi_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                429,
                headers={"X-Tps-trace-ID": "trace-123"},
                json={"code": 22009, "message": "msg limit exceed"},
            )

        async def token_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(
                200,
                json={"access_token": "abc", "expires_in": 7200},
            )

        openapi_client = httpx.AsyncClient(
            transport=httpx.MockTransport(openapi_handler),
            base_url="https://api.sgroup.qq.com",
        )
        token_client = httpx.AsyncClient(
            transport=httpx.MockTransport(token_handler),
        )
        provider = QQTokenProvider(
            QQConfig(appid="app", secret="secret"),
            client=token_client,
        )
        openapi = QQOpenAPI(QQConfig(), provider, client=openapi_client)

        try:
            await openapi.post("/test", json_body={"ping": "pong"})
        except Exception as exc:
            assert "trace_id=trace-123" in str(exc)
            assert "code=22009" in str(exc)
            assert "category=rate_limit" in str(exc)
            assert "msg limit exceed" in str(exc)
        else:
            raise AssertionError("expected openapi request to fail")

        await openapi_client.aclose()
        await token_client.aclose()

    asyncio.run(_run())


def test_known_openapi_error_catalog_contains_reply_expired() -> None:
    known = lookup_known_error(304027)

    assert known is not None
    assert known.name == "MSG_EXPIRE"
    assert known.category == "reply"
    assert known.retryable is False
