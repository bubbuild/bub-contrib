from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bub import hookimpl
from bub.types import State
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub_codex.utils import with_bub_skills

if TYPE_CHECKING:
    from bub.builtin.agent import Agent

THREADS_FILE = ".bub-codex-threads.json"
HANDOFF_SIGNAL_FILE = ".bub-codex-handoff.json"

_CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}(?:length|window)|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long",
    re.IGNORECASE,
)


def _load_session_data(session_id: str, state: State) -> dict[str, Any] | None:
    workspace = workspace_from_state(state)
    threads_file = workspace / THREADS_FILE
    with contextlib.suppress(FileNotFoundError):
        with threads_file.open() as f:
            threads = json.load(f)
        value = threads.get(session_id)
        if value is None:
            return None
        if isinstance(value, str):
            return {"thread_id": value, "anchor_count": 0}
        return value
    return None


def _save_session_data(session_id: str, data: dict[str, Any], state: State) -> None:
    workspace = workspace_from_state(state)
    threads_file = workspace / THREADS_FILE
    if threads_file.exists():
        with threads_file.open() as f:
            threads = json.load(f)
    else:
        threads = {}
    threads[session_id] = data
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


def _format_continuation(anchor_state: dict[str, Any]) -> str:
    parts: list[str] = ["[Continuation from previous phase]"]
    if summary := anchor_state.get("summary"):
        parts.append(f"Summary: {summary}")
    if next_steps := anchor_state.get("next_steps"):
        parts.append(f"Next steps: {next_steps}")
    return "\n".join(parts) if len(parts) > 1 else ""


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
    agent = _runtime_agent_from_state(state)
    tape_name: str | None = None
    thread_id: str | None = None

    if agent is not None:
        tape = agent.tapes.session_tape(session_id, workspace)
        tape_name = tape.name
        await agent.tapes.ensure_bootstrap_anchor(tape_name)
        info = await agent.tapes.info(tape_name)

        session_data = _load_session_data(session_id, state)
        stored_anchor_count = (session_data or {}).get("anchor_count", 0)

        if info.anchors > stored_anchor_count:
            thread_id = None
        else:
            thread_id = (session_data or {}).get("thread_id")

        # Inject continuation context on fresh session after handoff
        if thread_id is None:
            anchors = await agent.tapes.anchors(tape_name)
            if anchors and anchors[-1].name != "session/start":
                continuation = _format_continuation(anchors[-1].state)
                if continuation:
                    prompt = f"{continuation}\n\n---\n\n{prompt}"

        await agent.tapes.append_event(tape_name, "codex.run.start", {
            "thread_id": thread_id,
        })
    else:
        session_data = _load_session_data(session_id, state)
        thread_id = (session_data or {}).get("thread_id") if session_data else None

    command = ["codex", "e"]
    if thread_id:
        command.extend(["resume", thread_id])
    if codex_settings.model:
        command.extend(["--model", codex_settings.model])
    if codex_settings.yolo_mode:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    command.append(prompt)

    start = time.monotonic()
    with with_bub_skills(workspace):
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
        )
        stdout, stderr = await process.communicate()
    elapsed_ms = int((time.monotonic() - start) * 1000)

    output_blocks: list[str] = []
    new_thread_id: str | None = None
    if stdout:
        output_blocks.append(stdout.decode())
    if stderr:
        stderr_text = stderr.decode()
        for line in stderr_text.splitlines():
            if line.startswith("session id:"):
                new_thread_id = line.split(":", 1)[1].strip()
                break

    # Context-length error detection
    if process.returncode != 0 and stderr:
        stderr_text = stderr.decode()
        if _CONTEXT_LENGTH_PATTERNS.search(stderr_text):
            new_thread_id = None
            if agent is not None and tape_name:
                await agent.tapes.append_event(tape_name, "codex.run.context_overflow", {
                    "stderr": stderr_text[:500],
                })

    if agent is not None and tape_name:
        info = await agent.tapes.info(tape_name)
        _save_session_data(session_id, {
            "thread_id": new_thread_id,
            "anchor_count": info.anchors,
        }, state)
        await agent.tapes.append_event(tape_name, "codex.run.finish", {
            "thread_id": new_thread_id,
            "exit_code": process.returncode,
            "elapsed_ms": elapsed_ms,
        })
    elif new_thread_id:
        _save_session_data(session_id, {
            "thread_id": new_thread_id,
            "anchor_count": 0,
        }, state)

    return "\n".join(output_blocks)


@hookimpl
async def save_state(session_id: str, state: State, message: Any, model_output: str) -> None:
    workspace = workspace_from_state(state)
    signal_path = workspace / HANDOFF_SIGNAL_FILE
    if not signal_path.exists():
        return

    agent = _runtime_agent_from_state(state)
    if agent is None:
        signal_path.unlink(missing_ok=True)
        return

    try:
        with signal_path.open() as f:
            signal = json.load(f)
    except (json.JSONDecodeError, OSError):
        signal_path.unlink(missing_ok=True)
        return

    signal_path.unlink(missing_ok=True)

    tape = agent.tapes.session_tape(session_id, workspace)
    handoff_name = signal.get("name", "codex-handoff")
    handoff_state = {k: v for k, v in signal.items() if k != "name" and v}
    await agent.tapes.handoff(tape.name, name=handoff_name, state=handoff_state)
    _save_session_data(session_id, {"thread_id": None, "anchor_count": None}, state)
