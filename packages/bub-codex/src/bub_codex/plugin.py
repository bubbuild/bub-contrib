import asyncio
import contextlib
import importlib
from pathlib import Path
from typing import Generator, Literal

from bub import hookimpl
from bub.types import State
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SandboxMode = Literal["read-only", "workspace-write", "danger-full-access"]


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


def _copy_bub_skills(workspace: Path) -> list[Path]:
    bub_skill_paths = importlib.import_module("bub_skills").__path__
    collected_symlinks: list[Path] = []
    for skill_root in bub_skill_paths:
        for skill_dir in Path(skill_root).iterdir():
            if skill_dir.joinpath("SKILL.md").is_file():
                symlink_path = workspace.joinpath(skill_dir.name)
                if not symlink_path.exists():
                    symlink_path.symlink_to(skill_dir, target_is_directory=True)
                    collected_symlinks.append(symlink_path)
    return collected_symlinks


@contextlib.contextmanager
def with_bub_skills(workspace: Path) -> Generator[None, None, None]:
    """Context manager to copy bub skills into the workspace."""
    skills = _copy_bub_skills(workspace)
    try:
        yield
    finally:
        for skill in skills:
            with contextlib.suppress(OSError):
                skill.unlink()


@hookimpl
async def run_model(prompt: str, session_id: str, state: State) -> str:
    workspace = workspace_from_state(state)
    command = [
        "codex",
        "e",
        "resume",
        session_id,
    ]
    if codex_settings.model:
        command.extend(["--model", codex_settings.model])
    if codex_settings.yolo_mode:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    command.append("-")
    with with_bub_skills(workspace):
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
        )
        stdout, stderr = await process.communicate(prompt.encode())
    output_blocks: list[str] = []
    if stdout:
        output_blocks.append(stdout.decode())
    if stderr and process.returncode != 0:
        output_blocks.append(f"stderr: {stderr.decode()}")
    return "\n".join(output_blocks)
