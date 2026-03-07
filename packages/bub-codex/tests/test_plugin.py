from __future__ import annotations

import asyncio
import contextlib
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
    calls: list[tuple[tuple[object, ...], str | None]] = []

    class FakeProcess:
        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"codex-output\n", b"")

    async def fake_create_subprocess_exec(*args, stdout=None, cwd=None):
        calls.append((args, cwd))
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(plugin, "with_bub_skills", lambda workspace: contextlib.nullcontext())

    state = {"_runtime_workspace": str(tmp_path)}
    result = asyncio.run(plugin.run_model("hello", session_id="session-2", state=state))

    assert result == "codex-output\n"
    assert calls
    args, cwd = calls[0]
    assert args[:2] == ("codex", "e")
    assert args[-1] == "hello"
    assert cwd == str(tmp_path)
