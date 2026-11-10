"""Register validated plugin skills with Bub's existing skill discovery."""

from __future__ import annotations

import importlib
import tempfile
from pathlib import Path


class PluginSkillRegistry:
    """Expose only validated skill directories through Bub's skills namespace."""

    def __init__(self) -> None:
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._view_root: Path | None = None

    def activate(self, skill_directories: list[Path]) -> Path | None:
        self.close()
        if not skill_directories:
            return None

        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="bub-agent-plugin-skills-", ignore_cleanup_errors=True
        )
        view_root = Path(self._temporary_directory.name)
        for index, skill_directory in enumerate(skill_directories):
            plugin_view = view_root / str(index)
            plugin_view.mkdir()
            (plugin_view / skill_directory.name).symlink_to(
                skill_directory, target_is_directory=True
            )
            self._append_namespace_path(plugin_view)
        self._view_root = view_root
        return view_root

    def close(self) -> None:
        if self._view_root is not None:
            skills_package = importlib.import_module("skills")
            view_root = self._view_root
            skills_package.__path__ = [
                path
                for path in skills_package.__path__
                if not Path(path).is_relative_to(view_root)
            ]
            self._view_root = None
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    @staticmethod
    def _append_namespace_path(path: Path) -> None:
        skills_package = importlib.import_module("skills")
        current_paths = list(skills_package.__path__)
        text_path = str(path)
        if text_path not in current_paths:
            skills_package.__path__ = [*current_paths, text_path]
