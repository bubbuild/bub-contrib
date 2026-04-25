from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, cast

import bub
from bub import hookimpl
from bub.types import State
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub_kimi.utils import with_bub_skills

if TYPE_CHECKING:
    from bub.builtin.agent import Agent

THREADS_FILE = ".bub-kimi-threads.json"
RESUME_LINE_PREFIX = "To resume this session:"


def _load_thread_id(session_id: str, state: State) -> str | None:
    workspace = workspace_from_state(state)
    threads_file = workspace / THREADS_FILE
    with contextlib.suppress(FileNotFoundError):
        with threads_file.open() as f:
            threads = json.load(f)
        return threads.get(session_id)


def _save_thread_id(session_id: str, thread_id: str, state: State) -> None:
    workspace = workspace_from_state(state)
    threads_file = workspace / THREADS_FILE
    if threads_file.exists():
        with threads_file.open() as f:
            threads = json.load(f)
    else:
        threads = {}
    threads[session_id] = thread_id
    with threads_file.open("w") as f:
        json.dump(threads, f, indent=2)


def workspace_from_state(state: State) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


@bub.config(name="kimi")
class KimiSettings(bub.Settings):
    """Configuration for Kimi plugin."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_KIMI_", env_file=".env", extra="ignore"
    )
    model_name: str | None = Field(default=None)
    api_key: str | None = Field(default=None)
    base_url: str | None = Field(default=None)


def _settings() -> KimiSettings:
    return bub.ensure_config(KimiSettings)


def _runtime_agent_from_state(state: State) -> Agent | None:
    agent = state.get("_runtime_agent")
    if agent is None:
        return None
    return cast("Agent", agent)


async def _run_internal_command(prompt: str, session_id: str, state: State) -> str | None:
    if not prompt.strip().startswith(","):
        return None
    agent = _runtime_agent_from_state(state)
    if agent is None:
        return None
    return await agent.run(session_id=session_id, prompt=prompt, state=state)


@hookimpl
async def run_model(prompt: str, session_id: str, state: State) -> str:
    internal_command_result = await _run_internal_command(prompt, session_id, state)
    if internal_command_result is not None:
        return internal_command_result

    workspace = workspace_from_state(state)
    thread_id = _load_thread_id(session_id, state)
    command: list[str] = ["kimi"]
    if thread_id:
        command.extend(["-r", thread_id])
    command.append("--quiet")
    command.extend(["-p", prompt])
    env = os.environ.copy()
    settings = _settings()
    if settings.api_key:
        env["KIMI_API_KEY"] = settings.api_key
    if settings.base_url:
        env["KIMI_BASE_URL"] = settings.base_url
    if settings.model_name:
        env["KIMI_MODEL_NAME"] = settings.model_name
    with with_bub_skills(workspace):
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            env=env,
        )
        stdout, stderr = await process.communicate()

    stdout_text = stdout.decode() if stdout else ""
    stderr_text = stderr.decode() if stderr else ""

    stderr_lines: list[str] = []
    for line in stderr_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(RESUME_LINE_PREFIX):
            new_thread_id = stripped.rsplit(" ", 1)[-1]
            if new_thread_id:
                _save_thread_id(session_id, new_thread_id, state)
            continue
        stderr_lines.append(line)

    if process.returncode != 0:
        parts = [f"Kimi process exited with code {process.returncode}."]
        filtered_stderr = "\n".join(stderr_lines).strip()
        if filtered_stderr:
            parts.append(filtered_stderr)
        if stdout_text.strip():
            parts.append(stdout_text)
        return "\n\n".join(parts)

    return stdout_text
