from __future__ import annotations

import asyncio

import pytest
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import Protocol

from bub_qq.config import QQConfig
from bub_qq.protocol.auth import QQTokenProvider
from bub_qq.protocol.errors import QQOpenAPIError
from bub_qq.protocol.errors import lookup_known_error
from bub_qq.protocol.openapi import QQOpenAPI


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


def test_openapi_posts_group_text_message() -> None:
    async def _run() -> None:
        captured: dict[str, object] = {}

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            captured["path"] = request.url
            captured["json"] = request.json
            return FakeResponse(status=200, payload={"id": "reply-2", "timestamp": 123})

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

        payload = await openapi.post_group_text_message(
            group_openid="group-openid",
            content="hello group",
            msg_id="group-message-1",
            msg_seq=1,
        )

        assert payload["id"] == "reply-2"
        assert captured["path"] == "/v2/groups/group-openid/messages"
        assert captured["json"] == {
            "content": "hello group",
            "msg_type": 0,
            "msg_id": "group-message-1",
            "msg_seq": 1,
        }

    asyncio.run(_run())


def test_openapi_posts_c2c_markdown_message() -> None:
    async def _run() -> None:
        captured: dict[str, object] = {}

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            captured["path"] = request.url
            captured["json"] = request.json
            return FakeResponse(
                status=200, payload={"id": "reply-md", "timestamp": 123}
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

        payload = await openapi.post_c2c_markdown_message(
            openid="user-openid",
            content="**hello**",
            msg_id="message-1",
            msg_seq=2,
        )

        assert payload["id"] == "reply-md"
        assert captured["path"] == "/v2/users/user-openid/messages"
        assert captured["json"] == {
            "msg_type": 2,
            "markdown": {"content": "**hello**"},
            "msg_id": "message-1",
            "msg_seq": 2,
        }

    asyncio.run(_run())


def test_openapi_posts_group_markdown_message() -> None:
    async def _run() -> None:
        captured: dict[str, object] = {}

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            captured["path"] = request.url
            captured["json"] = request.json
            return FakeResponse(
                status=200, payload={"id": "reply-gmd", "timestamp": 123}
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

        payload = await openapi.post_group_markdown_message(
            group_openid="group-openid",
            content="**hello group**",
            msg_id="group-message-1",
            msg_seq=1,
        )

        assert payload["id"] == "reply-gmd"
        assert captured["path"] == "/v2/groups/group-openid/messages"
        assert captured["json"] == {
            "msg_type": 2,
            "markdown": {"content": "**hello group**"},
            "msg_id": "group-message-1",
            "msg_seq": 1,
        }

    asyncio.run(_run())


def test_openapi_posts_c2c_file_and_media_message() -> None:
    async def _run() -> None:
        captured: list[dict[str, object]] = []

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            captured.append({"path": request.url, "json": request.json})
            if str(request.url).endswith("/files"):
                return FakeResponse(
                    status=200,
                    payload={"file_uuid": "uuid-1", "file_info": "FILEINFO", "ttl": 300},
                )
            return FakeResponse(status=200, payload={"id": "media-1"})

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

        uploaded = await openapi.post_c2c_file(
            openid="user-openid",
            file_type=1,
            url="https://example.com/pic.png",
            file_name="pic.png",
        )
        sent = await openapi.post_c2c_media_message(
            openid="user-openid",
            file_info="FILEINFO",
            msg_id="message-1",
            msg_seq=1,
        )

        assert uploaded["file_info"] == "FILEINFO"
        assert sent["id"] == "media-1"
        assert captured[0] == {
            "path": "/v2/users/user-openid/files",
            "json": {
                "file_type": 1,
                "url": "https://example.com/pic.png",
                "srv_send_msg": False,
                "file_name": "pic.png",
            },
        }
        assert captured[1] == {
            "path": "/v2/users/user-openid/messages",
            "json": {
                "msg_type": 7,
                "media": {"file_info": "FILEINFO"},
                "msg_id": "message-1",
                "msg_seq": 1,
            },
        }

    asyncio.run(_run())


def test_openapi_posts_group_file() -> None:
    async def _run() -> None:
        captured: dict[str, object] = {}

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            captured["path"] = request.url
            captured["json"] = request.json
            return FakeResponse(
                status=200, payload={"file_uuid": "uuid-2", "file_info": "GFILE"}
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

        payload = await openapi.post_group_file(
            group_openid="group-openid",
            file_type=2,
            url="https://example.com/clip.mp4",
        )

        assert payload["file_info"] == "GFILE"
        assert captured["path"] == "/v2/groups/group-openid/files"
        assert captured["json"] == {
            "file_type": 2,
            "url": "https://example.com/clip.mp4",
            "srv_send_msg": False,
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


def test_openapi_refetches_token_once_after_401() -> None:
    async def _run() -> None:
        tokens = iter(["stale", "fresh", "never"])
        seen: list[str] = []

        async def token_handler(url: str, kwargs: dict[str, object]) -> FakeResponse:
            del url, kwargs
            return FakeResponse(
                status=200, payload={"access_token": next(tokens), "expires_in": 7200}
            )

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            seen.append(request.headers["Authorization"])
            if request.headers["Authorization"] == "QQBot stale":
                return FakeResponse(status=401, payload={"code": 11244})
            return FakeResponse(status=200, payload={"ok": True})

        provider = QQTokenProvider(
            QQConfig(appid="app", secret="secret", receive_mode="webhook"),
            client=FakeTokenClient(token_handler),
        )
        openapi = QQOpenAPI(
            QQConfig(receive_mode="webhook"),
            provider,
            client=FakeOpenAPIClient(openapi_handler),
        )

        assert await openapi.post("/test") == {"ok": True}
        assert seen == ["QQBot stale", "QQBot fresh"]
        assert await openapi.post("/test") == {"ok": True}
        assert seen[-1] == "QQBot fresh"  # cached again, no third token

    asyncio.run(_run())


def test_openapi_gives_up_after_second_401() -> None:
    async def _run() -> None:
        async def token_handler(url: str, kwargs: dict[str, object]) -> FakeResponse:
            del url, kwargs
            return FakeResponse(status=200, payload={"access_token": "t", "expires_in": 7200})

        calls = {"n": 0}

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            del request
            calls["n"] += 1
            return FakeResponse(status=401, payload={"code": 11244})

        openapi = QQOpenAPI(
            QQConfig(receive_mode="webhook"),
            QQTokenProvider(
                QQConfig(appid="app", secret="secret", receive_mode="webhook"),
                client=FakeTokenClient(token_handler),
            ),
            client=FakeOpenAPIClient(openapi_handler),
        )
        with pytest.raises(QQOpenAPIError):
            await openapi.post("/test")
        assert calls["n"] == 2

    asyncio.run(_run())


def test_replies_to_events_use_event_id() -> None:
    async def _run() -> None:
        bodies: list[dict] = []

        async def token_handler(url: str, kwargs: dict[str, object]) -> FakeResponse:
            del url, kwargs
            return FakeResponse(status=200, payload={"access_token": "t", "expires_in": 7200})

        async def openapi_handler(request: OpenAPIRequest) -> FakeResponse:
            bodies.append(dict(request.json or {}))
            return FakeResponse(status=200, payload={"id": "ok"})

        openapi = QQOpenAPI(
            QQConfig(receive_mode="webhook"),
            QQTokenProvider(
                QQConfig(appid="app", secret="secret", receive_mode="webhook"),
                client=FakeTokenClient(token_handler),
            ),
            client=FakeOpenAPIClient(openapi_handler),
        )
        await openapi.post_group_text_message(
            group_openid="g", content="hi", msg_id="event:click-1", msg_seq=1
        )
        await openapi.post_group_markdown_message(
            group_openid="g", content="**hi**", msg_id="msg-1", msg_seq=2
        )

        assert bodies[0]["event_id"] == "click-1" and "msg_id" not in bodies[0]
        assert bodies[1]["msg_id"] == "msg-1" and "event_id" not in bodies[1]

    asyncio.run(_run())
