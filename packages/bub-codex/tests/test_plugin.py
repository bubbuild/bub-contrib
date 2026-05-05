from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from bub_codex import plugin


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


def test_run_model_uses_codex_for_normal_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"codex-output\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-2", state=state))

    assert result == "codex-output\n"
    assert calls
    args, kwargs = calls[0]
    assert args[:2] == ("codex", "e")
    assert args[-1] == "hello"
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["stdout"] == asyncio.subprocess.PIPE
    assert kwargs["stderr"] == asyncio.subprocess.PIPE


def test_run_model_saves_session_id_from_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"codex-output\n",
                b"booting\nsession id: thread-123\nconnected\n",
            )

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-3", state=state))

    assert result == "codex-output\n"
    threads_file = tmp_path / plugin.THREADS_FILE
    data = json.loads(threads_file.read_text())
    assert data["session-3"]["thread_id"] == "thread-123"
    assert data["session-3"]["anchor_count"] == 0
