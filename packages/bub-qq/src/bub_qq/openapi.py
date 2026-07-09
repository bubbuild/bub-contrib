from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from typing import Protocol

import aiohttp

from .auth import QQTokenProvider
from .config import QQConfig
from .openapi_errors import build_openapi_error
from .openapi_errors import QQOpenAPIError
from .openapi_errors import trace_id_from_response


class ResponseLike(Protocol):
    status: int
    reason: str
    headers: Mapping[str, str]
    payload: Any


class OpenAPIHTTPClient(Protocol):
    async def request(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> ResponseLike: ...


class QQOpenAPI:
    """Minimal QQ OpenAPI client using QQBot access_token auth."""

    def __init__(
        self,
        config: QQConfig,
        token_provider: QQTokenProvider,
        *,
        client: OpenAPIHTTPClient | None = None,
    ) -> None:
        self._config = config
        self._token_provider = token_provider
        self._client = client
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._config.timeout_seconds)
            self._session = aiohttp.ClientSession(
                base_url=self._config.openapi_base_url,
                timeout=timeout,
            )
        return self._session

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def get_access_token(self) -> str:
        return await self._token_provider.get_token()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Authorization": f"QQBot {await self.get_access_token()}",
            "Content-Type": "application/json",
        }
        if headers:
            request_headers.update(headers)

        response = await self._request(
            method=method,
            path=path,
            params=params,
            json_body=json_body,
            headers=request_headers,
        )
        payload = response.payload
        if response.status < 200 or response.status >= 300:
            raise build_openapi_error(response, payload)
        if response.status in {201, 202}:
            raise build_openapi_error(
                response,
                payload,
                default_message="qq openapi async success requires follow-up handling",
            )
        if response.status == 204:
            return {}
        if not isinstance(payload, dict):
            raise QQOpenAPIError(
                status_code=response.status,
                trace_id=trace_id_from_response(response),
                error_code=None,
                error_message=f"qq openapi response is not a JSON object: {payload!r}",
                response_body=payload,
            )
        return payload

    async def _request(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> ResponseLike:
        if self._client is not None:
            return await self._client.request(
                method=method,
                url=path,
                params=params,
                json=json_body,
                headers=headers,
            )

        session = await self._get_session()
        async with session.request(
            method=method,
            url=path,
            params=params,
            json=json_body,
            headers=headers,
        ) as response:
            return _QQResponse(
                status=response.status,
                reason=response.reason or "",
                headers=dict(response.headers),
                payload=await _maybe_json(response),
            )

    async def get(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.request("POST", path, json_body=json_body)

    async def post_c2c_text_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, Any]:
        return await self.post(
            f"/v2/users/{openid}/messages",
            json_body={
                "content": content,
                "msg_type": 0,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            },
        )


class _QQResponse:
    def __init__(
        self,
        *,
        status: int,
        reason: str,
        headers: dict[str, str],
        payload: Any,
    ) -> None:
        self.status = status
        self.reason = reason
        self.headers = headers
        self.payload = payload


async def _maybe_json(response: aiohttp.ClientResponse) -> Any:
    body = await response.read()
    if not body:
        return None
    try:
        return await response.json()
    except (aiohttp.ContentTypeError, ValueError):
        return body.decode(response.get_encoding() or "utf-8", errors="replace")
