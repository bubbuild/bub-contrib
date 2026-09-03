"""Discover and validate portable Agent Plugins."""

from __future__ import annotations

import os
from pathlib import Path

from bub import skills as bub_skills
from jsonschema import Draft202012Validator

from bub_agent_plugins.documents import read_json_object
from bub_agent_plugins.mcp import MCPConfigError, is_contained, load_mcp_servers
from bub_agent_plugins.models import (
    AgentPluginLoadResult,
    AgentPluginManifest,
    LoadedAgentPlugin,
)
from bub_agent_plugins.schemas import find_schema


class ManifestError(ValueError):
    """An error that rejects one plugin package."""


class SkillsError(ValueError):
    """An error that disables skills for one plugin package."""


class AgentPluginLoader:
    """Load Agent Plugins while isolating failures at specification boundaries."""

    def __init__(
        self,
        *,
        data_root: Path,
        skills_enabled: bool = True,
        mcp_enabled: bool = True,
    ) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.skills_enabled = skills_enabled
        self.mcp_enabled = mcp_enabled

    def load(self, roots: list[Path]) -> AgentPluginLoadResult:
        result = AgentPluginLoadResult()
        names: set[str] = set()
        resolved_roots: set[Path] = set()

        for candidate in roots:
            try:
                root = candidate.expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                result.diagnostics.append(
                    f"{candidate}: cannot resolve plugin root: {exc}"
                )
                continue
            if root in resolved_roots:
                continue
            resolved_roots.add(root)

            try:
                plugin, diagnostics = self._load_plugin(root)
            except ManifestError as exc:
                result.diagnostics.append(f"{root}: plugin rejected: {exc}")
                continue
            result.diagnostics.extend(diagnostics)
            if plugin.manifest.name in names:
                result.diagnostics.append(
                    f"{root}: duplicate plugin name '{plugin.manifest.name}' ignored"
                )
                continue
            names.add(plugin.manifest.name)
            result.plugins.append(plugin)

        return result

    def _load_plugin(self, root: Path) -> tuple[LoadedAgentPlugin, list[str]]:
        manifest, diagnostics = _load_manifest(root)
        plugin = LoadedAgentPlugin(manifest=manifest, diagnostics=diagnostics)

        if self.skills_enabled:
            try:
                plugin.skill_directories, skill_diagnostics = _load_skills(manifest)
            except (OSError, SkillsError) as exc:
                diagnostics.append(
                    f"{manifest.root / 'skills'}: skills component disabled: {exc}"
                )
            else:
                diagnostics.extend(skill_diagnostics)

        if self.mcp_enabled:
            try:
                plugin.mcp_servers, mcp_diagnostics = load_mcp_servers(
                    manifest, self.data_root
                )
            except (OSError, MCPConfigError) as exc:
                diagnostics.append(
                    f"{manifest.root / 'mcp.json'}: MCP component disabled: {exc}"
                )
            else:
                diagnostics.extend(mcp_diagnostics)

        return plugin, diagnostics


def _load_manifest(root: Path) -> tuple[AgentPluginManifest, list[str]]:
    if not root.is_dir():
        raise ManifestError("plugin root is not a directory")

    manifest_path = root / "plugin.json"
    if not manifest_path.is_file() or not is_contained(manifest_path, root):
        raise ManifestError("plugin.json is missing or escapes the plugin root")

    try:
        document = read_json_object(manifest_path)
    except (OSError, ValueError) as exc:
        raise ManifestError(f"invalid manifest: {exc}") from exc

    schema = find_schema(document.get("$schema"))
    if schema is None:
        raise ManifestError("unsupported manifest schema")

    diagnostics: list[str] = []
    fields = set(schema.manifest["properties"])
    unknown_fields = sorted(set(document) - fields)
    if unknown_fields:
        diagnostics.append(
            f"{manifest_path}: ignored unknown fields: {', '.join(unknown_fields)}"
        )

    validation_document = {
        key: value for key, value in document.items() if key in fields
    }
    if "extensions" in validation_document:
        if not isinstance(validation_document["extensions"], dict):
            diagnostics.append(f"{manifest_path}: ignored non-object extensions field")
        validation_document.pop("extensions")

    errors = sorted(
        Draft202012Validator(schema.manifest).iter_errors(validation_document),
        key=lambda item: list(item.path),
    )
    if errors:
        raise ManifestError(errors[0].message)

    return (
        AgentPluginManifest(
            name=str(validation_document["name"]),
            root=root,
            schema=schema,
            data=document,
        ),
        diagnostics,
    )


def _load_skills(
    manifest: AgentPluginManifest,
) -> tuple[list[Path], list[str]]:
    skills_root = manifest.root / "skills"
    if not skills_root.exists():
        return [], []
    if not skills_root.is_dir() or not is_contained(skills_root, manifest.root):
        raise SkillsError("skills component is not a contained directory")

    skill_directories: list[Path] = []
    diagnostics: list[str] = []
    for skill_dir in sorted(skills_root.iterdir(), key=lambda path: path.name):
        skill_file = skill_dir / bub_skills.SKILL_FILE_NAME
        if not skill_dir.is_dir() or not skill_file.exists():
            continue
        if not skill_file.is_file() or not _tree_is_contained(skill_dir, manifest.root):
            diagnostics.append(
                f"{skill_file}: skill skipped because a package path escapes the plugin root"
            )
            continue
        if bub_skills._read_skill(skill_dir, source="builtin") is None:
            diagnostics.append(f"{skill_file}: invalid Agent Skill skipped")
            continue
        skill_directories.append(skill_dir.resolve())
    return skill_directories, diagnostics


def _tree_is_contained(path: Path, root: Path) -> bool:
    if not is_contained(path, root):
        return False
    try:

        def raise_walk_error(error: OSError) -> None:
            raise error

        return all(
            is_contained(Path(current_root) / name, root)
            for current_root, directories, filenames in os.walk(
                path, followlinks=False, onerror=raise_walk_error
            )
            for name in (*directories, *filenames)
        )
    except OSError:
        return False


__all__ = [
    "AgentPluginLoader",
    "AgentPluginLoadResult",
    "LoadedAgentPlugin",
]
