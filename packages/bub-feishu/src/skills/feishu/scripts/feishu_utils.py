#!/usr/bin/env python3
"""Shared utilities for Feishu scripts"""

from typing import Any

import httpx

OPENAPI_BASE_URL = "https://open.larksuite.com/open-apis"
TOKEN_URL = f"{OPENAPI_BASE_URL}/auth/v3/tenant_access_token/internal"


def raise_for_api_error(payload: dict[str, Any], *, prefix: str) -> None:
    """Raise error if API response indicates failure."""
    if payload.get("code") == 0:
        return
    raise RuntimeError(f"{prefix}: {payload.get('msg') or 'unknown error'}")


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    """Get tenant access token from Feishu API."""
    with httpx.Client() as client:
        response = client.post(
            TOKEN_URL,
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        raise_for_api_error(payload, prefix="Failed to get token")
        return str(payload["tenant_access_token"])


def authorized_headers(token: str) -> dict[str, str]:
    """Create authorized headers for Feishu API requests."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def request_json(
    method: str,
    path: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make a JSON request to Feishu API."""
    with httpx.Client() as client:
        response = client.request(
            method,
            f"{OPENAPI_BASE_URL}{path}",
            headers=authorized_headers(token),
            params=params,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
