from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
from collections.abc import AsyncIterator, Generator
from pathlib import Path

from bub import hookimpl
from bub.types import State
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

THREADS_FILE = ".bub-kapy-threads.json"


def workspace_from_state(state: State) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _load_thread_id(session_id: str, state: State) -> str | None:
    workspace = workspace_from_state(state)
    threads_file = workspace / THREADS_FILE
    with contextlib.suppress(FileNotFoundError):
        with threads_file.open() as f:
            threads = json.load(f)
        value = threads.get(session_id)
        if isinstance(value, str) and value.strip():
            return value
    return None


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
        json.dump(threads, f, indent=2, sort_keys=True)


def _copy_bub_skills(workspace: Path) -> list[Path]:
    with contextlib.suppress(ImportError):
        import bub_skills

        workspace.joinpath(".agents/skills").mkdir(parents=True, exist_ok=True)
        collected_symlinks: list[Path] = []
        for skill_root in bub_skills.__path__:
            for skill_dir in Path(skill_root).iterdir():
                if skill_dir.joinpath("SKILL.md").is_file():
                    symlink_path = workspace / ".agents/skills" / skill_dir.name
                    if not symlink_path.exists():
                        symlink_path.symlink_to(skill_dir, target_is_directory=True)
                        collected_symlinks.append(symlink_path)
        return collected_symlinks
    return []


@contextlib.contextmanager
def with_bub_skills(workspace: Path, enabled: bool) -> Generator[None, None, None]:
    if not enabled:
        yield
        return
    skills = _copy_bub_skills(workspace)
    try:
        yield
    finally:
        for skill in skills:
            with contextlib.suppress(OSError):
                skill.unlink()


class KapySettings(BaseSettings):
    """Configuration for the Kapybara bub plugin."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_KAPY_", env_file=".env", extra="ignore"
    )

    command: str = Field(
        default="kapybara chat --json -",
        description="Shell-style command used to invoke the Kapybara agent.",
    )
    model: str | None = Field(default=None)
    yolo_mode: bool = Field(default=False)
    prompt_mode: str = Field(default="stdin")
    resume_format: str = Field(default="resume {thread_id}")
    bubble_wrap_prompt: bool = Field(default=True)
    copy_skills: bool = Field(default=True)


kapy_settings = KapySettings()


def _build_command(
    prompt: str, session_id: str, state: State, settings: KapySettings
) -> tuple[list[str], bytes | None]:
    workspace = workspace_from_state(state)
    thread_id = _load_thread_id(session_id, state)
    command = shlex.split(settings.command)
    if not command:
        raise ValueError("BUB_KAPY_COMMAND must not be empty.")

    if thread_id and settings.resume_format.strip():
        command.extend(shlex.split(settings.resume_format.format(thread_id=thread_id)))
    if settings.model:
        command.extend(["--model", settings.model])
    if settings.yolo_mode:
        command.append("--dangerously-bypass-approvals-and-sandbox")

    prompt_text = prompt
    if settings.bubble_wrap_prompt:
        prompt_text = (
            "You are Kapybara operating through bub. Reply as Kapybara and treat "
            "this as the active user request.\n\n"
            f"{prompt}"
        )

    if settings.prompt_mode == "argv":
        command.append(prompt_text)
        stdin = None
    elif settings.prompt_mode == "stdin":
        stdin = prompt_text.encode()
    else:
        raise ValueError("BUB_KAPY_PROMPT_MODE must be either 'stdin' or 'argv'.")

    if "-" in command and stdin is None:
        command.remove("-")

    # Resolve relative executable paths from the runtime workspace.
    if command[0].startswith("."):
        command[0] = str((workspace / command[0]).resolve())
    return command, stdin


def _extract_thread_id(output: str) -> tuple[str | None, str]:
    lines = output.splitlines()
    if not lines:
        return None, output
    first_line = lines[0].strip()
    if not first_line:
        return None, output
    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(first_line)
        if isinstance(payload, dict):
            for key in ("thread_id", "session_id", "conversation_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    remaining = "\n".join(lines[1:]).strip()
                    return value, remaining
    return None, output


async def _invoke_kapybara(prompt: str, session_id: str, state: State) -> str:
    workspace = workspace_from_state(state)
    try:
        command, stdin = _build_command(prompt, session_id, state, kapy_settings)
    except ValueError as exc:
        return f"bub-kapy configuration error: {exc}"

    try:
        with with_bub_skills(workspace, kapy_settings.copy_skills):
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )
            stdout, stderr = await process.communicate(stdin)
    except FileNotFoundError:
        return (
            "bub-kapy could not start the Kapybara runtime. "
            f"Configured command not found: {command[0]!r}. "
            "Set BUB_KAPY_COMMAND to the installed Kapybara CLI."
        )

    stdout_text = stdout.decode() if stdout else ""
    stderr_text = stderr.decode() if stderr else ""

    thread_id, cleaned_stdout = _extract_thread_id(stdout_text)
    if thread_id:
        _save_thread_id(session_id, thread_id, state)

    output_blocks = [block for block in (cleaned_stdout, stderr_text.strip()) if block]
    if process.returncode:
        output_blocks.append(f"Kapybara process exited with code {process.returncode}.")
    return "\n".join(output_blocks).strip()


class KapyModel:
    """Compatibility wrapper for direct tests and ad-hoc use."""

    def __init__(self, session_id: str = "default", state: State | None = None) -> None:
        self.session_id = session_id
        self.state = state or {}

    async def run_model(self, prompt: str) -> AsyncIterator[str]:
        yield await _invoke_kapybara(prompt, self.session_id, self.state)


@hookimpl
async def run_model(prompt: str, session_id: str, state: State) -> str:
    return await _invoke_kapybara(prompt, session_id, state)
