from __future__ import annotations

import asyncio
import os
import shutil

CURSOR_API_KEY_ENV = "CURSOR_API_KEY"
CURSOR_CLI_CANDIDATES = ("cursor-agent", "agent")
CURSOR_AUTH_ERROR_MESSAGE = (
    "No Cursor authentication found. Run `bub login cursor` first or set "
    "`CURSOR_API_KEY`."
)


class CursorLoginError(RuntimeError):
    """Raised when Cursor CLI login cannot be started or completed."""


def resolve_cursor_cli_path(cli_path: str | None = None) -> str:
    if cli_path and cli_path.strip():
        return cli_path
    for candidate in CURSOR_CLI_CANDIDATES:
        if resolved := shutil.which(candidate):
            return resolved
    return CURSOR_CLI_CANDIDATES[0]


async def run_cursor_login(cli_path: str) -> int:
    try:
        process = await asyncio.create_subprocess_exec(cli_path, "login")
    except FileNotFoundError as exc:
        raise CursorLoginError(f"Cursor CLI not found: {cli_path}") from exc
    return await process.wait()


def has_cursor_api_key() -> bool:
    return bool(os.getenv(CURSOR_API_KEY_ENV, "").strip())


async def has_cursor_cli_login(cli_path: str, *, timeout_seconds: float = 10.0) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            cli_path,
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise CursorLoginError(f"Cursor CLI not found: {cli_path}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        return False
    output = f"{stdout.decode(errors='ignore')}\n{stderr.decode(errors='ignore')}"
    return process.returncode == 0 and "logged in as" in output.lower()


async def ensure_cursor_authenticated(cli_path: str) -> None:
    if has_cursor_api_key():
        return
    if await has_cursor_cli_login(cli_path):
        return
    raise RuntimeError(CURSOR_AUTH_ERROR_MESSAGE)


__all__ = [
    "CURSOR_AUTH_ERROR_MESSAGE",
    "CURSOR_CLI_CANDIDATES",
    "CursorLoginError",
    "ensure_cursor_authenticated",
    "has_cursor_api_key",
    "has_cursor_cli_login",
    "resolve_cursor_cli_path",
    "run_cursor_login",
]
