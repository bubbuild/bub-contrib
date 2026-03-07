import asyncio
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from bub import hookimpl
from bub.types import State
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub_codex.utils import with_bub_skills

if TYPE_CHECKING:
    from bub.builtin.agent import Agent

THREADS_FILE = ".bub-codex-threads.json"


def _load_thread_id(session_id: str, state: State) -> str | None:
    workpace = workspace_from_state(state)
    threads_file = workpace / THREADS_FILE
    with contextlib.suppress(FileNotFoundError):
        with threads_file.open() as f:
            threads = json.load(f)
        return threads.get(session_id)


def _save_thread_id(session_id: str, thread_id: str, state: State) -> None:
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


def workspace_from_state(state: State) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


class CodexSettings(BaseSettings):
    """Configuration for Codex plugin."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_CODEX_", env_file=".env", extra="ignore"
    )
    model: str | None = Field(default=None)
    yolo_mode: bool = False


codex_settings = CodexSettings()


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
    command = ["codex", "e"]
    if thread_id:
        command.extend(["resume", thread_id])
    if codex_settings.model:
        command.extend(["--model", codex_settings.model])
    if codex_settings.yolo_mode:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    command.append(prompt)
    with with_bub_skills(workspace):
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            cwd=str(workspace),
        )
        stdout, _ = await process.communicate()
    output_blocks: list[str] = []
    if stdout:
        output_blocks.append(stdout.decode())
        first_line = stdout.decode().splitlines()[0]
        if "thread_id" in first_line:
            thread_id = json.loads(first_line)["thread_id"]
            _save_thread_id(session_id, thread_id, state)
    return "\n".join(output_blocks)
