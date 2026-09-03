from __future__ import annotations

import json
from pathlib import Path

import pytest

from bub_agent_plugins.loader import AgentPluginLoader
from bub_agent_plugins.schemas import MCP_SCHEMA_ID, PLUGIN_SCHEMA_ID


def _write_manifest(root: Path, name: str = "example-plugin", **extra: object) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": name, **extra}),
        encoding="utf-8",
    )


def _write_skill(root: Path, name: str = "example-skill") -> Path:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Example skill\n---\nUse this skill.\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_mcp(root: Path, servers: dict[str, object], **extra: object) -> None:
    (root / "mcp.json").write_text(
        json.dumps({"$schema": MCP_SCHEMA_ID, "mcpServers": servers, **extra}),
        encoding="utf-8",
    )


def test_loads_skills_and_adapts_each_supported_mcp_transport(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(
        root,
        extensions={"com.example.ignored": "not validated"},
        unknown="reported but ignored",
    )
    skill_dir = _write_skill(root)
    (root / "bin").mkdir()
    (root / "bin" / "server").touch()
    _write_mcp(
        root,
        {
            "local": {
                "type": "stdio",
                "command": "./bin/server",
                "args": ["--root", "${PLUGIN_ROOT}", "${UNKNOWN}"],
                "env": {"CACHE": "${PLUGIN_DATA}/cache"},
                "cwd": "${PLUGIN_DATA}/work",
            },
            "remote": {
                "type": "streamable-http",
                "url": "https://example.com/mcp",
                "headers": {"X-Tenant": "public"},
            },
            "legacy": {
                "type": "sse",
                "url": "http://127.0.0.1:9000/events",
            },
        },
    )

    result = AgentPluginLoader(data_root=tmp_path / "data").load([root])

    assert result.skill_directories == [skill_dir.resolve()]
    assert set(result.mcp_servers) == {
        "example-plugin.local",
        "example-plugin.remote",
        "example-plugin.legacy",
    }
    local = result.mcp_servers["example-plugin.local"]
    assert local["command"] == str((root / "bin" / "server").resolve())
    assert local["args"] == ["--root", str(root.resolve()), "${UNKNOWN}"]
    assert local["env"] == {
        "CACHE": str(tmp_path / "data" / "example-plugin" / "cache"),
        "PLUGIN_ROOT": str(root.resolve()),
        "PLUGIN_DATA": str(tmp_path / "data" / "example-plugin"),
    }
    assert local["cwd"] == str(tmp_path / "data" / "example-plugin" / "work")
    assert result.mcp_servers["example-plugin.remote"]["transport"] == (
        "streamable-http"
    )
    assert result.mcp_servers["example-plugin.legacy"]["transport"] == "sse"
    assert any("ignored unknown fields" in item for item in result.diagnostics)


def test_invalid_mcp_file_does_not_disable_valid_skills(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root)
    skill_dir = _write_skill(root)
    _write_mcp(
        root,
        {"remote": {"type": "streamable-http", "url": "https://example.com"}},
        unexpected=True,
    )

    result = AgentPluginLoader(data_root=tmp_path / "data").load([root])

    assert result.skill_directories == [skill_dir.resolve()]
    assert result.mcp_servers == {}
    assert any("MCP component disabled" in item for item in result.diagnostics)


@pytest.mark.parametrize(
    ("name", "config", "message"),
    [
        (
            "insecure",
            {"type": "streamable-http", "url": "http://example.com/mcp"},
            "must use HTTPS",
        ),
        (
            "bad-command",
            {"type": "stdio", "command": "../server"},
            "bare executable",
        ),
        (
            "bad-cwd",
            {"type": "stdio", "command": "python", "cwd": "./../outside"},
            "escapes",
        ),
        (
            "headers",
            {
                "type": "streamable-http",
                "url": "https://example.com/mcp",
                "headers": {"X-Test": "one", "x-test": "two"},
            },
            "case-insensitive",
        ),
    ],
)
def test_invalid_mcp_entry_is_skipped_without_hiding_other_servers(
    tmp_path: Path, name: str, config: dict[str, object], message: str
) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root)
    _write_mcp(
        root,
        {
            name: config,
            "good": {"type": "stdio", "command": "python"},
        },
    )

    result = AgentPluginLoader(data_root=tmp_path / "data").load([root])

    assert set(result.mcp_servers) == {"example-plugin.good"}
    assert any(message in item for item in result.diagnostics)


def test_invalid_skill_does_not_disable_valid_mcp_server(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root)
    skill_dir = root / "skills" / "bad-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("missing frontmatter", encoding="utf-8")
    _write_mcp(root, {"good": {"type": "stdio", "command": "python"}})

    result = AgentPluginLoader(data_root=tmp_path / "data").load([root])

    assert result.skill_directories == []
    assert set(result.mcp_servers) == {"example-plugin.good"}
    assert any("invalid Agent Skill" in item for item in result.diagnostics)


def test_skill_with_escaping_symlink_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root)
    skill_dir = _write_skill(root)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (skill_dir / "secret.txt").symlink_to(outside)

    result = AgentPluginLoader(data_root=tmp_path / "data").load([root])

    assert result.skill_directories == []
    assert any("escapes the plugin root" in item for item in result.diagnostics)


def test_invalid_manifest_rejects_all_components(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write_manifest(root, name="Invalid Name")
    _write_skill(root)
    _write_mcp(root, {"good": {"type": "stdio", "command": "python"}})

    result = AgentPluginLoader(data_root=tmp_path / "data").load([root])

    assert result.plugins == []
    assert result.skill_directories == []
    assert result.mcp_servers == {}
    assert any("manifest rejected" in item for item in result.diagnostics)
