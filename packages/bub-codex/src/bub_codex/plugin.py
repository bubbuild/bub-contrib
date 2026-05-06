from __future__ import annotations

import asyncio
import os
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

_CONTEXT_LENGTH_PATTERNS = re.compile(
    r"context.{0,20}(?:length|window)|maximum.{0,20}context|token.{0,10}limit|prompt.{0,10}too long",
    re.IGNORECASE,
)


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


async def _get_thread_id_from_tape(agent: Agent, tape_name: str) -> str | None:
    """Get the thread_id from the most recent codex.thread event that belongs to the current anchor."""
    anchors = await agent.tapes.anchors(tape_name)
    current_anchor = anchors[-1].name if anchors else None

    tape = agent.tapes._llm.tape(tape_name)
    entries = list(await tape.query_async.last_anchor().all())
    for entry in reversed(entries):
        if entry.kind == "event" and entry.payload.get("name") == "codex.thread":
            data = entry.payload.get("data", {})
            if data.get("anchor") == current_anchor:
                return data.get("thread_id")
    return None


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

    command = ["codex", "e"]
    if codex_settings.model:
        command.extend(["--model", codex_settings.model])
    if codex_settings.yolo_mode:
        command.append("--dangerously-bypass-approvals-and-sandbox")

    start = time.monotonic()
    env = {
        **os.environ,
        "BUB_SESSION_ID": session_id,
        "BUB_BRIDGE_URL": "http://127.0.0.1:9800",
    }

    async with agent.tapes.fork_tape(tape_name, merge_back=True) if agent and tape_name else _noop_context():
        if agent and tape_name:
            await agent.tapes.ensure_bootstrap_anchor(tape_name)

            # Get thread_id from the latest anchor's subsequent events
            thread_id = await _get_thread_id_from_tape(agent, tape_name)

            # Inject continuation context when starting a fresh thread after handoff
            if thread_id is None:
                anchors = await agent.tapes.anchors(tape_name)
                if anchors and anchors[-1].name != "session/start":
                    continuation = _format_continuation(anchors[-1].state)
                    if continuation:
                        prompt = f"{continuation}\n\n---\n\n{prompt}"

            if thread_id:
                command.extend(["resume", thread_id])

        command.append(prompt)

        if agent and tape_name:
            await agent.tapes.append_event(tape_name, "codex.run.start", {
                "thread_id": thread_id,
            })

        with with_bub_skills(workspace):
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
                env=env,
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
                if agent and tape_name:
                    await agent.tapes.append_event(tape_name, "codex.run.context_overflow", {
                        "stderr": stderr_text[:500],
                    })

        # Save thread_id as event in tape (within fork, will be merged back)
        if agent and tape_name:
            anchors = await agent.tapes.anchors(tape_name)
            current_anchor = anchors[-1].name if anchors else None
            await agent.tapes.append_event(tape_name, "codex.thread", {
                "thread_id": new_thread_id,
                "anchor": current_anchor,
            })
            await agent.tapes.append_event(tape_name, "codex.run.finish", {
                "thread_id": new_thread_id,
                "exit_code": process.returncode,
                "elapsed_ms": elapsed_ms,
            })

    return "\n".join(output_blocks)


import contextlib
from collections.abc import AsyncGenerator


@contextlib.asynccontextmanager
async def _noop_context() -> AsyncGenerator[None, None]:
    yield