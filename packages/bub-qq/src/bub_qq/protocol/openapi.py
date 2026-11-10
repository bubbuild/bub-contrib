from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from typing import Protocol

import aiohttp

from ..config import QQConfig
from .auth import QQTokenProvider
from .errors import QQOpenAPIError
from .errors import build_openapi_error
from .errors import trace_id_from_response


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
        for attempt in range(2):
            token = await self.get_access_token()
            request_headers = {
                "Authorization": f"QQBot {token}",
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
            if response.status != 401 or attempt:
                break
            # The cached token was revoked or expired early: fetch a new one
            # and retry once instead of failing until its nominal expiry.
            self._token_provider.invalidate(token)
        payload = response.payload
        if response.status < 200 or response.status >= 300:
            raise build_openapi_error(response, payload)
        if response.status in {201, 202}:
            raise build_openapi_error(
                response,
                payload,
                default_message="qq openapi async success requires follow-up handling",
            )
        if response.status == 204 or payload is None:
            return {}
        if isinstance(payload, str) and not payload.strip():
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
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._post_text_message(
            path=f"/v2/users/{openid}/messages",
            content=content,
            msg_id=msg_id,
            msg_seq=msg_seq,
            keyboard=keyboard,
        )

    async def post_c2c_markdown_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._post_markdown_message(
            path=f"/v2/users/{openid}/messages",
            content=content,
            msg_id=msg_id,
            msg_seq=msg_seq,
            keyboard=keyboard,
        )

    async def post_group_text_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._post_text_message(
            path=f"/v2/groups/{group_openid}/messages",
            content=content,
            msg_id=msg_id,
            msg_seq=msg_seq,
            keyboard=keyboard,
        )

    async def post_group_markdown_message(
        self,
        *,
        group_openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._post_markdown_message(
            path=f"/v2/groups/{group_openid}/messages",
            content=content,
            msg_id=msg_id,
            msg_seq=msg_seq,
            keyboard=keyboard,
        )

    async def post_group_active_text_message(
        self,
        *,
        group_openid: str,
        content: str,
    ) -> dict[str, Any]:
        """Send a proactive group message (no ``msg_id``/``msg_seq``).

        Consumes the group's active-message quota and requires the group
        admin to have allowed proactive messages in the QQ client.
        """

        return await self.post(
            f"/v2/groups/{group_openid}/messages",
            json_body={"content": content, "msg_type": 0},
        )

    async def post_c2c_file(
        self,
        *,
        openid: str,
        file_type: int,
        url: str | None = None,
        file_name: str | None = None,
        upload_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post_file(
            path=f"/v2/users/{openid}/files",
            file_type=file_type,
            url=url,
            file_name=file_name,
            upload_id=upload_id,
        )

    async def post_group_file(
        self,
        *,
        group_openid: str,
        file_type: int,
        url: str | None = None,
        file_name: str | None = None,
        upload_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post_file(
            path=f"/v2/groups/{group_openid}/files",
            file_type=file_type,
            url=url,
            file_name=file_name,
            upload_id=upload_id,
        )

    async def post_c2c_upload_prepare(
        self, *, openid: str, **body: Any
    ) -> dict[str, Any]:
        return await self.post(f"/v2/users/{openid}/upload_prepare", json_body=body)

    async def post_group_upload_prepare(
        self, *, group_openid: str, **body: Any
    ) -> dict[str, Any]:
        return await self.post(
            f"/v2/groups/{group_openid}/upload_prepare", json_body=body
        )

    async def post_c2c_upload_part_finish(
        self, *, openid: str, **body: Any
    ) -> dict[str, Any]:
        return await self.post(
            f"/v2/users/{openid}/upload_part_finish", json_body=body
        )

    async def post_group_upload_part_finish(
        self, *, group_openid: str, **body: Any
    ) -> dict[str, Any]:
        return await self.post(
            f"/v2/groups/{group_openid}/upload_part_finish", json_body=body
        )

    async def put_url(self, url: str, data: bytes) -> None:
        """HTTP PUT a chunk to a presigned COS URL (absolute)."""

        session = await self._get_session()
        async with session.put(
            url,
            data=data,
            headers={"Content-Type": "application/octet-stream"},
        ) as response:
            wrapped = _QQResponse(
                status=response.status,
                reason=response.reason or "",
                headers=dict(response.headers),
                payload=await _maybe_json(response),
            )
            if wrapped.status < 200 or wrapped.status >= 300:
                raise build_openapi_error(wrapped, wrapped.payload)

    async def post_c2c_media_message(
        self,
        *,
        openid: str,
        file_info: str,
        msg_id: str,
        msg_seq: int,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._post_media_message(
            path=f"/v2/users/{openid}/messages",
            file_info=file_info,
            msg_id=msg_id,
            msg_seq=msg_seq,
            keyboard=keyboard,
        )

    async def post_group_media_message(
        self,
        *,
        group_openid: str,
        file_info: str,
        msg_id: str,
        msg_seq: int,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._post_media_message(
            path=f"/v2/groups/{group_openid}/messages",
            file_info=file_info,
            msg_id=msg_id,
            msg_seq=msg_seq,
            keyboard=keyboard,
        )

    async def _post_file(
        self,
        *,
        path: str,
        file_type: int,
        url: str | None,
        file_name: str | None,
        upload_id: str | None = None,
    ) -> dict[str, Any]:
        json_body: dict[str, Any] = {
            "file_type": file_type,
            "srv_send_msg": False,
        }
        if url:
            json_body["url"] = url
        if upload_id:
            json_body["upload_id"] = upload_id
        if file_name:
            json_body["file_name"] = file_name
        return await self.post(path, json_body=json_body)

    async def _post_media_message(
        self,
        *,
        path: str,
        file_info: str,
        msg_id: str,
        msg_seq: int,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.post(
            path,
            json_body=_with_keyboard(
                {
                    "msg_type": 7,
                    "media": {"file_info": file_info},
                    **reply_fields(msg_id),
                    "msg_seq": msg_seq,
                },
                keyboard,
            ),
        )

    async def _post_text_message(
        self,
        *,
        path: str,
        content: str,
        msg_id: str,
        msg_seq: int,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.post(
            path,
            json_body=_with_keyboard(
                {
                    "content": content,
                    "msg_type": 0,
                    **reply_fields(msg_id),
                    "msg_seq": msg_seq,
                },
                keyboard,
            ),
        )

    async def _post_markdown_message(
        self,
        *,
        path: str,
        content: str,
        msg_id: str,
        msg_seq: int,
        keyboard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.post(
            path,
            json_body=_with_keyboard(
                {
                    "msg_type": 2,
                    "markdown": {"content": content},
                    **reply_fields(msg_id),
                    "msg_seq": msg_seq,
                },
                keyboard,
            ),
        )

    async def put_interaction(
        self,
        *,
        interaction_id: str,
        code: int = 0,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        json_body: dict[str, Any] = {"code": code}
        if data:
            json_body["data"] = data
        return await self.request(
            "PUT",
            f"/interactions/{interaction_id}",
            json_body=json_body,
        )


def _with_keyboard(
    body: dict[str, Any], keyboard: dict[str, Any] | None
) -> dict[str, Any]:
    if keyboard:
        body["keyboard"] = keyboard
    return body


EVENT_REPLY_PREFIX = "event:"


def event_reply_id(event_id: str) -> str:
    """Reply id for an event (e.g. a button click) rather than a message."""

    return f"{EVENT_REPLY_PREFIX}{event_id}"


def reply_fields(reply_id: str) -> dict[str, str]:
    """``msg_id`` for messages, ``event_id`` for events.

    QQ answers button clicks (INTERACTION_CREATE) through ``event_id``; a
    click id passed as ``msg_id`` is rejected as an invalid message id.
    """

    if reply_id.startswith(EVENT_REPLY_PREFIX):
        return {"event_id": reply_id.removeprefix(EVENT_REPLY_PREFIX)}
    return {"msg_id": reply_id}


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
    text = body.decode(response.get_encoding() or "utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text
