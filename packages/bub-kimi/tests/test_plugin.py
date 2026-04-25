from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from bub_kimi import plugin


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def run(
        self, *, session_id: str, prompt: str, state: dict[str, object]
    ) -> str:
        self.calls.append((session_id, prompt, state))
        return "internal-command-result"


def test_run_model_delegates_internal_commands_to_runtime_agent() -> None:
    state: dict[str, object] = {"_runtime_agent": FakeAgent()}

    result = asyncio.run(plugin.run_model(",help", session_id="session-1", state=state))

    agent = state["_runtime_agent"]
    assert result == "internal-command-result"
    assert isinstance(agent, FakeAgent)
    assert agent.calls == [("session-1", ",help", state)]


def test_run_model_uses_kimi_for_normal_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"kimi-output\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-2", state=state))

    assert result == "kimi-output\n"
    assert calls
    args, kwargs = calls[0]
    assert args[0] == "kimi"
    assert "--quiet" in args
    assert args[-2:] == ("-p", "hello")
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE


def test_run_model_forwards_api_key_to_kimi_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"ok\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())
    monkeypatch.setattr(
        plugin,
        "_settings",
        lambda: plugin.KimiSettings(api_key="sk-test-123"),
    )

    state = {"_runtime_workspace": str(tmp_path)}
    asyncio.run(plugin.run_model("hello", session_id="session-api", state=state))

    _, kwargs = calls[0]
    env = kwargs["env"]
    assert env["KIMI_API_KEY"] == "sk-test-123"


def test_run_model_forwards_model_name_to_kimi_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"ok\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())
    monkeypatch.setattr(
        plugin,
        "_settings",
        lambda: plugin.KimiSettings(model_name="kimi-k2"),
    )

    state = {"_runtime_workspace": str(tmp_path)}
    asyncio.run(plugin.run_model("hello", session_id="session-model", state=state))

    args, kwargs = calls[0]
    assert "--model" not in args
    assert kwargs["env"]["KIMI_MODEL_NAME"] == "kimi-k2"


def test_run_model_forwards_base_url_to_kimi_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"ok\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())
    monkeypatch.setattr(
        plugin,
        "_settings",
        lambda: plugin.KimiSettings(base_url="https://kimi.example.com/v1"),
    )

    state = {"_runtime_workspace": str(tmp_path)}
    asyncio.run(plugin.run_model("hello", session_id="session-base-url", state=state))

    _, kwargs = calls[0]
    env = kwargs["env"]
    assert env["KIMI_BASE_URL"] == "https://kimi.example.com/v1"


def test_run_model_omits_api_key_when_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"ok\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())
    monkeypatch.setattr(plugin, "_settings", lambda: plugin.KimiSettings())
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_BASE_URL", raising=False)
    monkeypatch.delenv("KIMI_MODEL_NAME", raising=False)

    state = {"_runtime_workspace": str(tmp_path)}
    asyncio.run(plugin.run_model("hello", session_id="session-api-missing", state=state))

    _, kwargs = calls[0]
    assert "KIMI_API_KEY" not in kwargs["env"]
    assert "KIMI_BASE_URL" not in kwargs["env"]
    assert "KIMI_MODEL_NAME" not in kwargs["env"]


def test_run_model_saves_session_id_from_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"kimi-output\n",
                b"booting\nTo resume this session: kimi -r thread-123\n",
            )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-3", state=state))

    assert result == "kimi-output\n"
    threads_file = tmp_path / plugin.THREADS_FILE
    assert json.loads(threads_file.read_text()) == {"session-3": "thread-123"}


def test_run_model_resumes_thread_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    threads_file = tmp_path / plugin.THREADS_FILE
    threads_file.write_text(json.dumps({"session-4": "thread-abc"}))

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"ok\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    asyncio.run(plugin.run_model("again", session_id="session-4", state=state))

    args, _ = calls[0]
    assert args[0] == "kimi"
    assert args[1] == "-r"
    assert args[2] == "thread-abc"


def test_run_model_surfaces_stderr_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        returncode = 2

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"kimi: boom: auth required\n")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-5", state=state))

    assert "Kimi process exited with code 2." in result
    assert "auth required" in result


def test_run_model_filters_resume_line_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"partial\n",
                b"something failed\nTo resume this session: kimi -r thread-err\n",
            )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-6", state=state))

    assert "Kimi process exited with code 1." in result
    assert "something failed" in result
    assert "partial" in result
    assert "To resume this session" not in result
    threads_file = tmp_path / plugin.THREADS_FILE
    assert json.loads(threads_file.read_text()) == {"session-6": "thread-err"}
