from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from bub.runtime import AsyncStreamEvents, StreamEvent
from typer.testing import CliRunner

from bub.builtin.auth import app as auth_app
from bub_cursor import plugin


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def run_stream(
        self,
        *,
        session_id: str,
        prompt: str,
        state: dict[str, object],
    ) -> AsyncStreamEvents:
        self.calls.append((session_id, prompt, state))

        async def events():
            yield StreamEvent("text", {"delta": "internal-command"})
            yield StreamEvent("text", {"delta": "-result"})

        return AsyncStreamEvents(events())


async def _async_noop() -> None:
    return None


@pytest.fixture(autouse=True)
def clear_cursor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUB_CURSOR_CLI_PATH", raising=False)
    monkeypatch.delenv("BUB_CURSOR_MODEL", raising=False)
    monkeypatch.delenv("BUB_CURSOR_TIMEOUT_SECONDS", raising=False)


def test_run_model_delegates_internal_commands_to_runtime_agent() -> None:
    state: dict[str, object] = {"_runtime_agent": FakeAgent()}

    result = asyncio.run(plugin.run_model(",help", session_id="session-1", state=state))

    agent = state["_runtime_agent"]
    assert result == "internal-command-result"
    assert isinstance(agent, FakeAgent)
    assert agent.calls == [("session-1", ",help", state)]


def test_run_model_uses_cursor_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "cursor-output",
                        "session_id": "cursor-thread-1",
                    }
                ).encode(),
                b"",
            )

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        plugin, "ensure_cursor_authenticated", lambda cli_path: _async_noop()
    )
    monkeypatch.setattr(
        plugin,
        "resolve_cursor_cli_path",
        lambda cli_path=None: cli_path or "cursor-agent",
    )
    monkeypatch.setattr(
        plugin,
        "_settings",
        lambda: plugin.CursorSettings(
            model="cursor-model",
            cli_path="cursor-agent",
            timeout_seconds=12,
        ),
    )

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-1", state=state))

    assert result == "cursor-output"
    assert calls
    args, kwargs = calls[0]
    assert args == (
        "cursor-agent",
        "-p",
        "hello",
        "--output-format",
        "json",
        "--model",
        "cursor-model",
    )
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE
    assert json.loads((tmp_path / plugin.THREADS_FILE).read_text()) == {
        "session-1": "cursor-thread-1"
    }


def test_run_model_resumes_previous_cursor_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / plugin.THREADS_FILE).write_text(
        json.dumps({"session-1": "cursor-thread-1"})
    )
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b'{"result": "ok"}', b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        plugin, "ensure_cursor_authenticated", lambda cli_path: _async_noop()
    )
    monkeypatch.setattr(
        plugin,
        "resolve_cursor_cli_path",
        lambda cli_path=None: cli_path or "cursor-agent",
    )
    monkeypatch.setattr(plugin, "_settings", lambda: plugin.CursorSettings())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-1", state=state))

    assert result == "ok"
    args, _ = calls[0]
    assert args[:2] == ("cursor-agent", "--resume=cursor-thread-1")


def test_run_model_returns_process_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeProcess:
        returncode = 2

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"stdout text", b"stderr text")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        plugin, "ensure_cursor_authenticated", lambda cli_path: _async_noop()
    )
    monkeypatch.setattr(
        plugin,
        "resolve_cursor_cli_path",
        lambda cli_path=None: cli_path or "cursor-agent",
    )
    monkeypatch.setattr(plugin, "_settings", lambda: plugin.CursorSettings())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-1", state=state))

    assert "Cursor process exited with code 2." in result
    assert "stderr text" in result
    assert "stdout text" in result


def test_run_model_returns_plain_stdout_when_json_parse_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"plain output", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        plugin, "ensure_cursor_authenticated", lambda cli_path: _async_noop()
    )
    monkeypatch.setattr(
        plugin,
        "resolve_cursor_cli_path",
        lambda cli_path=None: cli_path or "cursor-agent",
    )
    monkeypatch.setattr(plugin, "_settings", lambda: plugin.CursorSettings())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-1", state=state))

    assert result == "plain output"


def test_run_model_wraps_cursor_cli_with_bub_skills(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, Path]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            events.append(("communicate", tmp_path))
            return (b'{"result": "ok"}', b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        events.append(("spawn", Path(str(kwargs["cwd"]))))
        return FakeProcess()

    @contextlib.contextmanager
    def fake_with_bub_skills(workspace: Path):
        events.append(("enter", workspace))
        try:
            yield
        finally:
            events.append(("exit", workspace))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", fake_with_bub_skills)
    monkeypatch.setattr(
        plugin, "ensure_cursor_authenticated", lambda cli_path: _async_noop()
    )
    monkeypatch.setattr(
        plugin,
        "resolve_cursor_cli_path",
        lambda cli_path=None: cli_path or "cursor-agent",
    )
    monkeypatch.setattr(plugin, "_settings", lambda: plugin.CursorSettings())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-1", state=state))

    assert result == "ok"
    assert events == [
        ("enter", tmp_path),
        ("spawn", tmp_path),
        ("communicate", tmp_path),
        ("exit", tmp_path),
    ]


def test_run_model_checks_cursor_auth_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def fake_ensure_cursor_authenticated(cli_path: str) -> None:
        calls.append(cli_path)
        raise RuntimeError(
            "No Cursor authentication found. Run `bub login cursor` first."
        )

    async def fake_create_subprocess_exec(*args, **kwargs):
        raise AssertionError("Cursor CLI should not be spawned when auth is missing")

    monkeypatch.setattr(
        plugin, "ensure_cursor_authenticated", fake_ensure_cursor_authenticated
    )
    monkeypatch.setattr(
        plugin,
        "resolve_cursor_cli_path",
        lambda cli_path=None: cli_path or "cursor-agent",
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(
        plugin,
        "_settings",
        lambda: plugin.CursorSettings(cli_path="cursor-agent"),
    )

    state = {"_runtime_workspace": str(tmp_path)}
    with pytest.raises(RuntimeError, match="bub login cursor"):
        asyncio.run(plugin.run_model("hello", session_id="session-1", state=state))

    assert calls == ["cursor-agent"]


def test_cursor_login_command_runs_agent_login(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_run_cursor_login(cli_path: str) -> int:
        calls.append(cli_path)
        return 0

    monkeypatch.setattr(plugin, "run_cursor_login", fake_run_cursor_login)
    monkeypatch.setattr(
        plugin,
        "resolve_cursor_cli_path",
        lambda cli_path=None: cli_path or "cursor-agent",
    )
    monkeypatch.setattr(
        plugin,
        "_settings",
        lambda: plugin.CursorSettings(cli_path="cursor-agent"),
    )

    result = CliRunner().invoke(auth_app, ["cursor"])

    assert result.exit_code == 0
    assert calls == ["cursor-agent"]
    assert "login: ok" in result.stdout


def test_cursor_login_command_accepts_cli_path_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_run_cursor_login(cli_path: str) -> int:
        calls.append(cli_path)
        return 0

    monkeypatch.setattr(plugin, "run_cursor_login", fake_run_cursor_login)
    monkeypatch.setattr(
        plugin,
        "resolve_cursor_cli_path",
        lambda cli_path=None: cli_path or "cursor-agent",
    )

    result = CliRunner().invoke(auth_app, ["cursor", "--cli-path", "custom-agent"])

    assert result.exit_code == 0
    assert calls == ["custom-agent"]


def test_cursor_login_command_returns_cli_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_cursor_login(cli_path: str) -> int:
        return 3

    monkeypatch.setattr(plugin, "run_cursor_login", fake_run_cursor_login)
    monkeypatch.setattr(
        plugin,
        "resolve_cursor_cli_path",
        lambda cli_path=None: cli_path or "cursor-agent",
    )

    result = CliRunner().invoke(auth_app, ["cursor", "--cli-path", "agent"])

    assert result.exit_code == 3
