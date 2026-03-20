from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import QQConfig


@dataclass(frozen=True)
class QQAccessToken:
    """Cached access token and its refresh boundary."""

    value: str
    expires_at: float

    def is_valid(self, *, now: float) -> bool:
        return now < self.expires_at


class QQAuthError(RuntimeError):
    """Raised when QQ token acquisition fails."""


class QQTokenProvider:
    """Fetch and cache QQ Open Platform access tokens."""

    def __init__(
        self,
        config: QQConfig,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._clock = clock or time.time
        self._token: QQAccessToken | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        now = float(self._clock())
        token = self._token
        if token is not None and token.is_valid(now=now):
            return token.value

        async with self._lock:
            now = float(self._clock())
            token = self._token
            if token is not None and token.is_valid(now=now):
                return token.value

            self._token = await self._request_new_token()
            return self._token.value

    async def _request_new_token(self) -> QQAccessToken:
        if not self._config.appid or not self._config.secret:
            raise QQAuthError("qq appid/secret is empty")

        response = await self._request_token()
        payload = response.json()

        access_token = str(payload.get("access_token") or "").strip()
        expires_in = payload.get("expires_in")
        if not access_token:
            raise QQAuthError(f"qq token response missing access_token: {payload}")

        try:
            expires_in_seconds = int(expires_in)
        except (TypeError, ValueError) as exc:
            raise QQAuthError(f"qq token response has invalid expires_in: {payload}") from exc

        refresh_after = max(expires_in_seconds - self._config.token_refresh_skew_seconds, 0)
        return QQAccessToken(
            value=access_token,
            expires_at=float(self._clock()) + refresh_after,
        )

    async def _request_token(self) -> httpx.Response:
        kwargs: dict[str, Any] = {
            "json": {
                "appId": self._config.appid,
                "clientSecret": self._config.secret,
            },
            "headers": {"Content-Type": "application/json"},
            "timeout": self._config.timeout_seconds,
        }
        if self._client is not None:
            response = await self._client.post(self._config.token_url, **kwargs)
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(self._config.token_url, **kwargs)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise QQAuthError(f"qq token request failed: {exc}") from exc
        return response
