from __future__ import annotations

import asyncio
import shutil

import pytest

from bub_cursor import auth


def test_resolve_cursor_cli_path_prefers_explicit_path() -> None:
    assert (
        auth.resolve_cursor_cli_path("/custom/cursor-agent") == "/custom/cursor-agent"
    )


def test_resolve_cursor_cli_path_prefers_cursor_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_which(command: str) -> str | None:
        return f"/bin/{command}" if command in {"cursor-agent", "agent"} else None

    monkeypatch.setattr(shutil, "which", fake_which)

    assert auth.resolve_cursor_cli_path() == "/bin/cursor-agent"


def test_resolve_cursor_cli_path_falls_back_to_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_which(command: str) -> str | None:
        return "/bin/agent" if command == "agent" else None

    monkeypatch.setattr(shutil, "which", fake_which)

    assert auth.resolve_cursor_cli_path() == "/bin/agent"


def test_resolve_cursor_cli_path_returns_cursor_agent_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda command: None)

    assert auth.resolve_cursor_cli_path() == "cursor-agent"


def test_run_cursor_login_spawns_agent_login(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeProcess:
        async def wait(self) -> int:
            return 0

    async def fake_create_subprocess_exec(*args):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(auth.run_cursor_login("cursor-agent"))

    assert result == 0
    assert calls == [("cursor-agent", "login")]


def test_run_cursor_login_wraps_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create_subprocess_exec(*args):
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(auth.CursorLoginError, match="Cursor CLI not found"):
        asyncio.run(auth.run_cursor_login("missing-agent"))


def test_ensure_cursor_authenticated_uses_cursor_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "cursor-api-key")

    async def fake_has_cursor_cli_login(cli_path: str) -> bool:
        raise AssertionError("status should not be called when CURSOR_API_KEY is set")

    monkeypatch.setattr(auth, "has_cursor_cli_login", fake_has_cursor_cli_login)

    asyncio.run(auth.ensure_cursor_authenticated("agent"))


def test_ensure_cursor_authenticated_uses_cli_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    calls: list[str] = []

    async def fake_has_cursor_cli_login(cli_path: str) -> bool:
        calls.append(cli_path)
        return True

    monkeypatch.setattr(auth, "has_cursor_cli_login", fake_has_cursor_cli_login)

    asyncio.run(auth.ensure_cursor_authenticated("cursor-agent"))

    assert calls == ["cursor-agent"]


def test_ensure_cursor_authenticated_raises_when_status_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)

    async def fake_has_cursor_cli_login(cli_path: str) -> bool:
        return False

    monkeypatch.setattr(auth, "has_cursor_cli_login", fake_has_cursor_cli_login)

    with pytest.raises(RuntimeError, match="bub login cursor"):
        asyncio.run(auth.ensure_cursor_authenticated("agent"))


def test_has_cursor_cli_login_runs_agent_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"Logged in as user@example.com", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(auth.has_cursor_cli_login("cursor-agent"))

    assert result is True
    assert calls == [("cursor-agent", "status")]


def test_has_cursor_cli_login_requires_logged_in_as_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"Authenticated", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(auth.has_cursor_cli_login("cursor-agent"))

    assert result is False
