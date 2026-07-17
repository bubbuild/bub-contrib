from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Protocol, cast

import bub
from bub import hookimpl
from bub.streaming import StreamEvent
from bub.turn import TurnState
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub_codex.utils import with_bub_skills

THREADS_FILE = ".bub-codex-threads.json"


class RuntimeAgent(Protocol):
    async def run_stream(
        self, *, session_id: str, prompt: str | list[dict], state: TurnState
    ) -> AsyncIterable[StreamEvent]: ...


def _load_thread_id(session_id: str, state: TurnState) -> str | None:
    workpace = workspace_from_state(state)
    threads_file = workpace / THREADS_FILE
    with contextlib.suppress(FileNotFoundError):
        with threads_file.open() as f:
            threads = json.load(f)
        return threads.get(session_id)


def _save_thread_id(session_id: str, thread_id: str, state: TurnState) -> None:
    workpace = workspace_from_state(state)
    threads_file = workpace / THREADS_FILE
    if threads_file.exists():
        with threads_file.open() as f:
            threads = json.load(f)
    else:
        threads = {}
    threads[session_id] = thread_id
    with threads_file.open("w") as f:
        json.dump(threads, f, indent=2)


def workspace_from_state(state: TurnState) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


@bub.config(name="codex")
class CodexSettings(bub.Settings):
    """Configuration for Codex plugin."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_CODEX_", env_file=".env", extra="ignore"
    )
    model: str | None = Field(default=None)
    yolo_mode: bool = False


def _settings() -> CodexSettings:
    return bub.ensure_config(CodexSettings)


def _runtime_agent_from_state(state: TurnState) -> RuntimeAgent | None:
    agent = state.get("_runtime_agent")
    if agent is None:
        return None
    return cast("RuntimeAgent", agent)


async def _run_internal_command(
    prompt: str, session_id: str, state: TurnState
) -> str | None:
    if not prompt.strip().startswith(","):
        return None
    agent = _runtime_agent_from_state(state)
    if agent is None:
        return None
    stream = await agent.run_stream(session_id=session_id, prompt=prompt, state=state)
    parts: list[str] = []
    async for event in stream:
        if event.kind == "text":
            parts.append(str(event.data.get("delta", "")))
    return "".join(parts)


@hookimpl
async def run_model(prompt: str, session_id: str, state: TurnState) -> str:
    internal_command_result = await _run_internal_command(prompt, session_id, state)
    if internal_command_result is not None:
        return internal_command_result

    workspace = workspace_from_state(state)
    thread_id = _load_thread_id(session_id, state)
    command = ["codex", "e"]
    if thread_id:
        command.extend(["resume", thread_id])
    settings = _settings()
    if settings.model:
        command.extend(["--model", settings.model])
    if settings.yolo_mode:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    command.append(prompt)
    with with_bub_skills(workspace):
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
        )
        stdout, stderr = await process.communicate()
    output_blocks: list[str] = []
    if stdout:
        output_blocks.append(stdout.decode())
    if stderr:
        stderr_text = stderr.decode()
        for line in stderr_text.splitlines():
            if line.startswith("session id:"):
                thread_id = line.split(":", 1)[1].strip()
                _save_thread_id(session_id, thread_id, state)
                break
    return "\n".join(output_blocks)
