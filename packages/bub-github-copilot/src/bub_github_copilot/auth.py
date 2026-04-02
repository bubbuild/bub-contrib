from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_GITHUB_COPILOT_OAUTH_CLIENT_ID = "Ov23li8tweQw6odWQebz"
DEFAULT_GITHUB_COPILOT_DEVICE_CODE_URL = "https://github.com/login/device/code"
DEFAULT_GITHUB_COPILOT_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105
DEFAULT_GITHUB_COPILOT_SCOPE = "read:user user:email"
DEFAULT_GITHUB_API_VERSION = "2022-11-28"
DEFAULT_GITHUB_HOST = "github.com"
TOKEN_PATH = Path.home() / ".bub" / "github_copilot_auth.json"
GITHUB_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
GITHUB_DEVICE_FLOW_EXPIRED_MESSAGE = (
    "GitHub device flow expired before authorization completed."
)
GITHUB_DEVICE_FLOW_DENIED_MESSAGE = "GitHub device flow was denied by the user."
GITHUB_DEVICE_FLOW_TIMEOUT_MESSAGE = (
    "GitHub device flow timed out before authorization completed."
)


class GitHubCopilotOAuthResponseError(TypeError):
    """Raised when GitHub Copilot OAuth payloads are malformed."""


class GitHubCopilotOAuthLoginError(RuntimeError):
    """Raised when GitHub Copilot OAuth login flow cannot complete."""


@dataclass(frozen=True)
class GitHubCopilotOAuthTokens:
    github_token: str
    github_token_type: str = "bearer"  # noqa: S105
    github_scope: str | None = None
    expires_at: int | None = None
    account_id: str | None = None
    login: str | None = None
    email: str | None = None


def _normalize_optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _parse_tokens(payload: dict[str, Any]) -> GitHubCopilotOAuthTokens | None:
    github_token = _normalize_optional_str(payload.get("github_token"))
    if github_token is None:
        return None
    return GitHubCopilotOAuthTokens(
        github_token=github_token,
        github_token_type=_normalize_optional_str(payload.get("github_token_type"))
        or "bearer",
        github_scope=_normalize_optional_str(payload.get("github_scope")),
        expires_at=_normalize_optional_int(payload.get("expires_at")),
        account_id=_normalize_optional_str(payload.get("account_id")),
        login=_normalize_optional_str(payload.get("login")),
        email=_normalize_optional_str(payload.get("email")),
    )


def load_github_copilot_oauth_tokens() -> GitHubCopilotOAuthTokens | None:
    try:
        payload = _load_json_object(TOKEN_PATH)
    except (OSError, json.JSONDecodeError, GitHubCopilotOAuthResponseError):
        return None
    return _parse_tokens(payload)


def save_github_copilot_oauth_tokens(tokens: GitHubCopilotOAuthTokens) -> Path:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "github_token": tokens.github_token,
        "github_token_type": tokens.github_token_type,
        "github_scope": tokens.github_scope,
        "expires_at": tokens.expires_at,
        "account_id": tokens.account_id,
        "login": tokens.login,
        "email": tokens.email,
        "updated_at": int(time.time()),
    }
    TOKEN_PATH.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with suppress(OSError):
        os.chmod(TOKEN_PATH, 0o600)
    return TOKEN_PATH


def load_github_token_from_env() -> str | None:
    for env_name in GITHUB_TOKEN_ENV_VARS:
        value = _normalize_optional_str(os.getenv(env_name))
        if value is not None:
            return value
    return None


def _parse_github_cli_hosts_yaml(contents: str, *, host: str) -> str | None:
    current_host: str | None = None
    for raw_line in contents.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0 and stripped.endswith(":"):
            current_host = stripped[:-1].strip()
            continue
        if current_host != host:
            continue
        if indent < 2 or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key.strip() != "oauth_token":
            continue
        token = value.strip().strip("'\"")
        return token or None
    return None


def load_github_cli_oauth_token(
    gh_config_dir: str | Path | None = None,
    *,
    host: str = DEFAULT_GITHUB_HOST,
) -> str | None:
    hosts_path = _gh_hosts_path(gh_config_dir)
    try:
        contents = hosts_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_github_cli_hosts_yaml(contents, host=host)


def load_github_cli_oauth_token_via_command(
    *,
    host: str = DEFAULT_GITHUB_HOST,
    timeout_seconds: float = 5.0,
) -> str | None:
    gh_path = shutil.which("gh")
    if gh_path is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [gh_path, "auth", "token", "--hostname", host],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _normalize_optional_str(result.stdout)


def resolve_github_copilot_token(
    *,
    gh_config_dir: str | Path | None = None,
    host: str = DEFAULT_GITHUB_HOST,
) -> str | None:
    stored = load_github_copilot_oauth_tokens()
    if stored is not None:
        return stored.github_token
    return (
        load_github_token_from_env()
        or load_github_cli_oauth_token(gh_config_dir, host=host)
        or load_github_cli_oauth_token_via_command(host=host)
    )


def _github_headers(*, token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "X-GitHub-Api-Version": DEFAULT_GITHUB_API_VERSION,
        "User-Agent": "bub-github-copilot-auth/0",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise GitHubCopilotOAuthResponseError
    return payload


def _gh_hosts_path(gh_config_dir: str | Path | None) -> Path:
    if gh_config_dir is not None:
        return Path(gh_config_dir).expanduser() / "hosts.yml"

    root = os.getenv("GH_CONFIG_DIR")
    config_dir = Path(root) if root else Path("~/.config/gh")
    return config_dir.expanduser() / "hosts.yml"


def _post_json(
    url: str,
    *,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.post(url, json=payload, headers=_github_headers())
    response.raise_for_status()
    return _load_response_json(response)


def _fetch_profile(github_token: str, *, timeout_seconds: float) -> dict[str, Any]:
    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.get(
            "https://api.github.com/user",
            headers=_github_headers(token=github_token),
        )
    response.raise_for_status()
    return _load_response_json(response)


def _load_response_json(response: httpx.Response) -> dict[str, Any]:
    body = response.json()
    if not isinstance(body, dict):
        raise GitHubCopilotOAuthResponseError
    return body


def _default_device_code_notifier(verification_uri: str, user_code: str) -> None:
    print(f"Open {verification_uri} and enter code: {user_code}")


def _poll_github_device_access_token(
    *,
    device_code: str,
    interval_seconds: int,
    expires_in_seconds: int,
    timeout_seconds: float,
    client_id: str,
    access_token_url: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + expires_in_seconds
    poll_interval = max(1, interval_seconds)
    while time.monotonic() < deadline:
        payload = _post_json(
            access_token_url,
            payload={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout_seconds=timeout_seconds,
        )
        if "access_token" in payload:
            return payload

        error = _normalize_optional_str(payload.get("error"))
        if error == "authorization_pending":
            time.sleep(poll_interval)
            continue
        if error == "slow_down":
            poll_interval += 5
            time.sleep(poll_interval)
            continue
        if error == "expired_token":
            raise GitHubCopilotOAuthLoginError(GITHUB_DEVICE_FLOW_EXPIRED_MESSAGE)
        if error == "access_denied":
            raise GitHubCopilotOAuthLoginError(GITHUB_DEVICE_FLOW_DENIED_MESSAGE)
        if error:
            message = _normalize_optional_str(payload.get("error_description")) or error
            raise GitHubCopilotOAuthLoginError(message)
        time.sleep(poll_interval)
    raise GitHubCopilotOAuthLoginError(GITHUB_DEVICE_FLOW_TIMEOUT_MESSAGE)


def login_github_copilot_oauth(
    *,
    open_browser: bool = True,
    browser_opener: Callable[[str], Any] | None = None,
    device_code_notifier: Callable[[str, str], Any] | None = None,
    scope: str = DEFAULT_GITHUB_COPILOT_SCOPE,
    timeout_seconds: float = 15.0,
    client_id: str = DEFAULT_GITHUB_COPILOT_OAUTH_CLIENT_ID,
    device_code_url: str = DEFAULT_GITHUB_COPILOT_DEVICE_CODE_URL,
    access_token_url: str = DEFAULT_GITHUB_COPILOT_ACCESS_TOKEN_URL,
) -> GitHubCopilotOAuthTokens:
    device_payload = _post_json(
        device_code_url,
        payload={"client_id": client_id, "scope": scope},
        timeout_seconds=timeout_seconds,
    )
    device_code = _normalize_optional_str(device_payload.get("device_code"))
    user_code = _normalize_optional_str(device_payload.get("user_code"))
    verification_uri = _normalize_optional_str(device_payload.get("verification_uri"))
    interval_seconds = _normalize_optional_int(device_payload.get("interval")) or 5
    expires_in_seconds = (
        _normalize_optional_int(device_payload.get("expires_in")) or 900
    )
    if device_code is None or user_code is None or verification_uri is None:
        raise GitHubCopilotOAuthResponseError

    if open_browser:
        opener = browser_opener or webbrowser.open
        opener(verification_uri)

    notifier = device_code_notifier or _default_device_code_notifier
    notifier(verification_uri, user_code)

    token_payload = _poll_github_device_access_token(
        device_code=device_code,
        interval_seconds=interval_seconds,
        expires_in_seconds=expires_in_seconds,
        timeout_seconds=timeout_seconds,
        client_id=client_id,
        access_token_url=access_token_url,
    )
    github_token = _normalize_optional_str(token_payload.get("access_token"))
    if github_token is None:
        raise GitHubCopilotOAuthResponseError

    expires_in = _normalize_optional_int(token_payload.get("expires_in"))
    expires_at = int(time.time() + expires_in) if expires_in is not None else None
    profile = _fetch_profile(github_token, timeout_seconds=timeout_seconds)
    tokens = GitHubCopilotOAuthTokens(
        github_token=github_token,
        github_token_type=_normalize_optional_str(token_payload.get("token_type"))
        or "bearer",
        github_scope=_normalize_optional_str(token_payload.get("scope")) or scope,
        expires_at=expires_at,
        account_id=str(profile.get("id")) if profile.get("id") is not None else None,
        login=_normalize_optional_str(profile.get("login")),
        email=_normalize_optional_str(profile.get("email")),
    )
    save_github_copilot_oauth_tokens(tokens)
    return tokens


__all__ = [
    "GitHubCopilotOAuthLoginError",
    "GitHubCopilotOAuthResponseError",
    "GitHubCopilotOAuthTokens",
    "load_github_cli_oauth_token",
    "load_github_cli_oauth_token_via_command",
    "load_github_copilot_oauth_tokens",
    "login_github_copilot_oauth",
    "resolve_github_copilot_token",
    "save_github_copilot_oauth_tokens",
    "TOKEN_PATH",
]
