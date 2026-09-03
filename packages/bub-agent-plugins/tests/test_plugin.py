from __future__ import annotations

import importlib.metadata
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from bub.skills import discover_skills

from bub_agent_plugins.plugin import AgentPluginsPlugin, AgentPluginsSettings
from bub_agent_plugins.schemas import MCP_SCHEMA_ID, PLUGIN_SCHEMA_ID


def _write_portable_plugin(root: Path, *, skill_name: str = "portable-skill") -> None:
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": "portable"}),
        encoding="utf-8",
    )
    skill_dir = root / "skills" / skill_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: Portable version\n---\nPortable body\n",
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA_ID,
                "mcpServers": {"local": {"type": "stdio", "command": "python"}},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("skills_enabled", "mcp_enabled"),
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_component_ablation_matrix(
    tmp_path: Path, skills_enabled: bool, mcp_enabled: bool
) -> None:
    plugin_root = tmp_path / "portable"
    _write_portable_plugin(plugin_root)
    framework = SimpleNamespace(workspace=tmp_path)
    settings = AgentPluginsSettings(
        paths=[plugin_root],
        auto_discover=False,
        skills_enabled=skills_enabled,
        mcp_enabled=mcp_enabled,
        data_root=tmp_path / "data",
    )
    plugin = AgentPluginsPlugin(framework, settings=settings)

    try:
        catalog = plugin.load()
        discovered = {skill.name for skill in discover_skills(tmp_path)}
        channels = plugin.provide_channels(lambda _message: None)  # type: ignore[arg-type]

        assert ("portable-skill" in discovered) is skills_enabled
        assert bool(catalog.skill_directories) is skills_enabled
        assert bool(catalog.mcp_servers) is mcp_enabled
        assert bool(channels) is mcp_enabled
        if channels:
            assert channels[0].name == "agent-plugins.mcp"
            assert channels[0]._read_mcp_servers() == catalog.mcp_servers
    finally:
        plugin._skills.close()


def test_workspace_skill_keeps_precedence_over_portable_skill(tmp_path: Path) -> None:
    plugin_root = tmp_path / "portable"
    _write_portable_plugin(plugin_root, skill_name="shared")
    workspace_skill = tmp_path / ".agents" / "skills" / "shared"
    workspace_skill.mkdir(parents=True)
    (workspace_skill / "SKILL.md").write_text(
        "---\nname: shared\ndescription: Workspace version\n---\nWorkspace body\n",
        encoding="utf-8",
    )
    plugin = AgentPluginsPlugin(
        SimpleNamespace(workspace=tmp_path),
        settings=AgentPluginsSettings(
            paths=[plugin_root], auto_discover=False, data_root=tmp_path / "data"
        ),
    )

    try:
        plugin.load()
        skill = next(
            item for item in discover_skills(tmp_path) if item.name == "shared"
        )
        assert skill.description == "Workspace version"
        assert skill.source == "project"
    finally:
        plugin._skills.close()


def test_auto_discovers_immediate_workspace_plugin(tmp_path: Path) -> None:
    plugin_root = tmp_path / ".agents" / "plugins" / "portable"
    _write_portable_plugin(plugin_root)
    plugin = AgentPluginsPlugin(
        SimpleNamespace(workspace=tmp_path),
        settings=AgentPluginsSettings(data_root=tmp_path / "data"),
    )

    try:
        catalog = plugin.load()
        assert [item.manifest.name for item in catalog.plugins] == ["portable"]
    finally:
        plugin._skills.close()


def test_entry_point_registered() -> None:
    entry_points = importlib.metadata.entry_points(group="bub")
    entry_point = next(item for item in entry_points if item.name == "agent-plugins")
    assert entry_point.value == "bub_agent_plugins.plugin:AgentPluginsPlugin"


def test_mcp_is_a_host_requirement_not_a_transitive_dependency() -> None:
    package_root = Path(__file__).parents[1]
    repository_root = Path(__file__).parents[3]
    package_project = tomllib.loads(
        (package_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    root_project = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert not any(
        dependency.startswith("bub-mcp")
        for dependency in package_project["project"]["dependencies"]
    )
    assert {"bub-agent-plugins", "bub-mcp"} <= set(
        root_project["project"]["dependencies"]
    )
