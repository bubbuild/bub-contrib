from __future__ import annotations

import json
from pathlib import Path

from bub_agent_plugins.loader import AgentPluginLoader, AgentPluginLoadResult
from bub_agent_plugins.schemas import MCP_SCHEMA_ID, PLUGIN_SCHEMA_ID


def _write_manifest(root: Path, name: str = "example-plugin") -> None:
    root.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": name}),
        encoding="utf-8",
    )


def _write_skill(root: Path, body: str | None = None) -> Path:
    skill_dir = root / "skills" / "example-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        body or "---\nname: example-skill\ndescription: Example skill\n---\nUse it.\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_mcp(root: Path, servers: dict[str, object], **extra: object) -> None:
    (root / "mcp.json").write_text(
        json.dumps({"$schema": MCP_SCHEMA_ID, "mcpServers": servers, **extra}),
        encoding="utf-8",
    )


def _load(root: Path, tmp_path: Path) -> AgentPluginLoadResult:
    return AgentPluginLoader(data_root=tmp_path / "data").load([root])


def test_invalid_manifest_is_not_loaded(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root, name="Invalid Name")
    _write_skill(root)

    assert _load(root, tmp_path).plugins == []


def test_invalid_mcp_does_not_hide_a_valid_skill(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root)
    skill_dir = _write_skill(root)
    _write_mcp(
        root,
        {"server": {"type": "stdio", "command": "python"}},
        unexpected=True,
    )

    result = _load(root, tmp_path)

    assert result.skill_directories == [skill_dir.resolve()]
    assert result.mcp_servers == {}


def test_invalid_skill_does_not_hide_a_valid_mcp_server(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root)
    _write_skill(root, body="missing frontmatter")
    _write_mcp(root, {"server": {"type": "stdio", "command": "python"}})

    result = _load(root, tmp_path)

    assert result.skill_directories == []
    assert set(result.mcp_servers) == {"example-plugin.server"}


def test_invalid_mcp_server_does_not_hide_another_server(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root)
    _write_mcp(
        root,
        {
            "insecure": {
                "type": "streamable-http",
                "url": "http://example.com/mcp",
            },
            "valid": {"type": "stdio", "command": "python"},
        },
    )

    assert set(_load(root, tmp_path).mcp_servers) == {"example-plugin.valid"}


def test_skill_cannot_escape_the_plugin_root(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root)
    skill_dir = _write_skill(root)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (skill_dir / "outside.txt").symlink_to(outside)

    assert _load(root, tmp_path).skill_directories == []


def test_plugin_data_cannot_escape_the_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root)
    _write_mcp(root, {"server": {"type": "stdio", "command": "python"}})
    data_root = tmp_path / "data"
    data_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_root / "example-plugin").symlink_to(outside, target_is_directory=True)

    result = _load(root, tmp_path)

    assert result.mcp_servers == {}
    assert any("PLUGIN_DATA escapes" in message for message in result.diagnostics)
