from __future__ import annotations

from typing import Any

import httpx

from .auth import QQTokenProvider
from .config import QQConfig
from .openapi_errors import build_openapi_error
from .openapi_errors import QQOpenAPIError
from .openapi_errors import trace_id_from_response


class QQOpenAPI:
    """Minimal QQ OpenAPI client using QQBot access_token auth."""

    def __init__(
        self,
        config: QQConfig,
        token_provider: QQTokenProvider,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._token_provider = token_provider
        self._client = client or httpx.AsyncClient(
            base_url=self._config.openapi_base_url,
            timeout=self._config.timeout_seconds,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

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

        response = await self._client.request(
            method=method,
            url=path,
            params=params,
            json=json_body,
            headers=request_headers,
        )
        payload = _maybe_json(response)
        if response.status_code < 200 or response.status_code >= 300:
            raise build_openapi_error(response, payload)
        if response.status_code in {201, 202}:
            raise build_openapi_error(
                response,
                payload,
                default_message="qq openapi async success requires follow-up handling",
            )
        if response.status_code == 204:
            return {}
        if not isinstance(payload, dict):
            raise QQOpenAPIError(
                status_code=response.status_code,
                trace_id=trace_id_from_response(response),
                error_code=None,
                error_message=f"qq openapi response is not a JSON object: {payload!r}",
                response_body=payload,
            )
        return payload

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
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


def _maybe_json(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text
