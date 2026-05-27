from __future__ import annotations

import contextlib
import importlib
from collections.abc import Generator
from pathlib import Path

SKILL_TARGET_DIR = ".agents/skills"


def _copy_bub_skills(workspace: Path) -> list[Path]:
    bub_skill_paths = importlib.import_module("skills").__path__
    workspace.joinpath(SKILL_TARGET_DIR).mkdir(parents=True, exist_ok=True)
    collected_symlinks: list[Path] = []
    for skill_root in bub_skill_paths:
        for skill_dir in Path(skill_root).iterdir():
            if skill_dir.joinpath("SKILL.md").is_file():
                symlink_path = workspace / SKILL_TARGET_DIR / skill_dir.name
                if not symlink_path.exists():
                    symlink_path.symlink_to(skill_dir, target_is_directory=True)
                    collected_symlinks.append(symlink_path)
    return collected_symlinks


def _safe_copy_bub_skills(workspace: Path) -> list[Path]:
    with contextlib.suppress(ModuleNotFoundError):
        return _copy_bub_skills(workspace)
    return []


@contextlib.contextmanager
def with_bub_skills(workspace: Path) -> Generator[None, None, None]:
    """Temporarily expose installed Bub packaged skills under workspace .agents/skills."""

    skills = _safe_copy_bub_skills(workspace)
    try:
        yield
    finally:
        for skill in skills:
            with contextlib.suppress(OSError):
                skill.unlink()
