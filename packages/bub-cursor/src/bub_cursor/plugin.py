from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Annotated, Any, Protocol, cast

import bub
import typer
from bub import BubFramework, hookimpl
from bub.builtin.auth import app as auth_app
from bub.runtime import StreamEvent
from bub.types import State
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub_cursor.auth import (
    CursorLoginError,
    ensure_cursor_authenticated,
    resolve_cursor_cli_path,
    run_cursor_login,
)
from bub_cursor.utils import with_bub_skills

THREADS_FILE = ".bub-cursor-threads.json"
CliPathOption = Annotated[
    str | None,
    typer.Option(
        "--cli-path",
        help="Cursor CLI executable path. Defaults to BUB_CURSOR_CLI_PATH or auto-detection.",
    ),
]


class RuntimeAgent(Protocol):
    async def run_stream(
        self, *, session_id: str, prompt: str | list[dict], state: State
    ) -> AsyncIterable[StreamEvent]: ...


@bub.config(name="cursor")
class CursorSettings(bub.Settings):
    """Configuration for the Cursor Bub plugin."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_CURSOR_",
        env_file=".env",
        extra="ignore",
    )

    model: str | None = None
    cli_path: str | None = None
    timeout_seconds: float = Field(default=300.0, gt=0)


def _settings() -> CursorSettings:
    return bub.ensure_config(CursorSettings)


def workspace_from_state(state: State) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _load_thread_id(session_id: str, state: State) -> str | None:
    threads_file = workspace_from_state(state) / THREADS_FILE
    with contextlib.suppress(FileNotFoundError, json.JSONDecodeError):
        with threads_file.open() as f:
            threads = json.load(f)
        thread_id = threads.get(session_id)
        if isinstance(thread_id, str) and thread_id:
            return thread_id
    return None


def _save_thread_id(session_id: str, thread_id: str, state: State) -> None:
    threads_file = workspace_from_state(state) / THREADS_FILE
    if threads_file.exists():
        with threads_file.open() as f:
            threads = json.load(f)
    else:
        threads = {}
    threads[session_id] = thread_id
    with threads_file.open("w") as f:
        json.dump(threads, f, indent=2)


def _runtime_agent_from_state(state: State) -> RuntimeAgent | None:
    agent = state.get("_runtime_agent")
    if agent is None:
        return None
    return cast("RuntimeAgent", agent)


def _prompt_to_text(prompt: str | list[dict[str, Any]]) -> str:
    if isinstance(prompt, str):
        return prompt
    return "\n".join(
        str(part.get("text", ""))
        for part in prompt
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


async def _run_internal_command(
    prompt: str, session_id: str, state: State
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


@auth_app.command(name="cursor")
def cursor_login(cli_path: CliPathOption = None) -> None:
    """Login with Cursor CLI browser authentication."""

    settings = _settings()
    command = resolve_cursor_cli_path(cli_path or settings.cli_path)
    try:
        exit_code = asyncio.run(run_cursor_login(command))
    except CursorLoginError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    if exit_code != 0:
        raise typer.Exit(exit_code)

    typer.echo("login: ok")


def _cursor_command(
    *,
    prompt: str,
    thread_id: str | None,
    settings: CursorSettings,
) -> list[str]:
    command = [resolve_cursor_cli_path(settings.cli_path)]
    if thread_id:
        command.append(f"--resume={thread_id}")
    command.extend(["-p", prompt, "--output-format", "json"])
    if settings.model:
        command.extend(["--model", settings.model])
    return command


def _result_from_stdout(stdout_text: str, session_id: str, state: State) -> str:
    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError:
        return stdout_text

    if thread_id := data.get("session_id"):
        if isinstance(thread_id, str) and thread_id:
            _save_thread_id(session_id, thread_id, state)

    result = data.get("result")
    if isinstance(result, str):
        return result
    if data.get("is_error"):
        return json.dumps(data, ensure_ascii=False)
    return stdout_text


@hookimpl
async def run_model(
    prompt: str | list[dict[str, Any]], session_id: str, state: State
) -> str:
    prompt_text = _prompt_to_text(prompt)
    internal_command_result = await _run_internal_command(
        prompt_text, session_id, state
    )
    if internal_command_result is not None:
        return internal_command_result

    workspace = workspace_from_state(state)
    settings = _settings()
    cli_path = resolve_cursor_cli_path(settings.cli_path)
    await ensure_cursor_authenticated(cli_path)
    command = _cursor_command(
        prompt=prompt_text,
        thread_id=_load_thread_id(session_id, state),
        settings=settings,
    )
    with with_bub_skills(workspace):
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=settings.timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return (
                f"Cursor process timed out after {settings.timeout_seconds:g} seconds."
            )

    stdout_text = stdout.decode() if stdout else ""
    stderr_text = stderr.decode() if stderr else ""

    if process.returncode != 0:
        parts = [f"Cursor process exited with code {process.returncode}."]
        if stderr_text.strip():
            parts.append(stderr_text)
        if stdout_text.strip():
            parts.append(stdout_text)
        return "\n\n".join(parts)

    return _result_from_stdout(stdout_text, session_id, state)


class CursorPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework


__all__ = [
    "CursorPlugin",
    "CursorSettings",
    "run_model",
    "workspace_from_state",
]
