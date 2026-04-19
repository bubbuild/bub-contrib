from __future__ import annotations

import contextlib
import importlib
from collections.abc import Generator
from pathlib import Path


def _copy_bub_skills(workspace: Path) -> list[Path]:
    bub_skill_paths = importlib.import_module("skills").__path__
    workspace.joinpath(".agents/skills").mkdir(parents=True, exist_ok=True)
    collected_symlinks: list[Path] = []
    for skill_root in bub_skill_paths:
        for skill_dir in Path(skill_root).iterdir():
            if skill_dir.joinpath("SKILL.md").is_file():
                symlink_path = workspace / ".agents/skills" / skill_dir.name
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
