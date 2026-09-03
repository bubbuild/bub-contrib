"""Exercise Agent Plugins through Bub's installed entry points in one process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-fixture", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--skills-enabled", type=int, choices=(0, 1), required=True)
    parser.add_argument("--mcp-enabled", type=int, choices=(0, 1), required=True)
    return parser.parse_args()


def _write_runtime_files(
    *, plugin_fixture: Path, workspace: Path, skills_enabled: bool, mcp_enabled: bool
) -> Path:
    plugin_root = workspace / ".agents" / "plugins" / plugin_fixture.name
    plugin_root.parent.mkdir(parents=True)
    shutil.copytree(plugin_fixture, plugin_root)

    skill_root = workspace / ".agents" / "skills" / "baseline-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: baseline-skill\n"
        "description: Verify existing Bub skill discovery.\n"
        "---\n"
        "Return `baseline-skill-ok`.\n",
        encoding="utf-8",
    )
    shared_skill_root = workspace / ".agents" / "skills" / "shared-skill"
    shared_skill_root.mkdir(parents=True)
    (shared_skill_root / "SKILL.md").write_text(
        "---\n"
        "name: shared-skill\n"
        "description: Workspace skill takes precedence.\n"
        "---\n"
        "Return `workspace-skill-ok`.\n",
        encoding="utf-8",
    )

    bub_home = Path(os.environ["BUB_HOME"])
    bub_home.mkdir(parents=True)
    (bub_home / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "baseline": {
                        "transport": "stdio",
                        "command": "python",
                        "args": [str(plugin_root / "server.py")],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config_file = bub_home / "config.yml"
    config_file.write_text(
        json.dumps(
            {
                "agent-plugins": {
                    "skills_enabled": skills_enabled,
                    "mcp_enabled": mcp_enabled,
                    "data_root": str(bub_home / "agent-plugins"),
                }
            }
        ),
        encoding="utf-8",
    )
    return config_file


async def _wait_for_tool(name: str, timeout: float = 30) -> Any:
    from bub.tools import REGISTRY

    async with asyncio.timeout(timeout):
        while name not in REGISTRY:
            await asyncio.sleep(0.05)
    return REGISTRY[name]


async def _exercise_channels(
    channels: dict[str, Any], mcp_enabled: bool
) -> dict[str, Any]:
    from bub.tools import REGISTRY

    baseline = channels["mcp.lifecycle"]
    plugin_channel = channels.get("agent-plugins.mcp")
    assert (plugin_channel is not None) is mcp_enabled

    active_channels = [baseline]
    if plugin_channel is not None:
        active_channels.append(plugin_channel)

    try:
        for channel in active_channels:
            await channel.start(asyncio.Event())

        baseline_tool = await _wait_for_tool("mcp.baseline_ping")
        baseline_result = await baseline_tool.run(value="baseline")
        assert baseline_result == "basic-mcp-ok:baseline"

        plugin_result = None
        if plugin_channel is not None:
            plugin_tool = await _wait_for_tool("mcp.basic-agent-plugin.basic_ping")
            plugin_result = await plugin_tool.run(value="plugin")
            assert plugin_result == "basic-mcp-ok:plugin"
        else:
            assert "mcp.basic-agent-plugin.basic_ping" not in REGISTRY

        return {
            "baseline_mcp": baseline_result,
            "plugin_mcp": plugin_result,
        }
    finally:
        for channel in reversed(active_channels):
            await channel.stop()


def main() -> None:
    args = _parse_args()
    plugin_fixture = args.plugin_fixture.resolve()
    workspace = args.workspace.resolve()
    skills_enabled = bool(args.skills_enabled)
    mcp_enabled = bool(args.mcp_enabled)
    workspace.mkdir(parents=True)
    config_file = _write_runtime_files(
        plugin_fixture=plugin_fixture,
        workspace=workspace,
        skills_enabled=skills_enabled,
        mcp_enabled=mcp_enabled,
    )
    os.chdir(workspace)

    from bub.framework import BubFramework
    from bub.skills import discover_skills

    framework = BubFramework(config_file=config_file)
    framework.load_hooks()

    async def message_handler(_message: Any) -> None:
        return None

    channels = framework.get_channels(message_handler)
    skills = {skill.name: skill for skill in discover_skills(workspace)}
    assert skills["baseline-skill"].body() == "Return `baseline-skill-ok`."
    assert skills["shared-skill"].body() == "Return `workspace-skill-ok`."
    assert ("basic-skill" in skills) is skills_enabled
    if skills_enabled:
        assert (
            skills["basic-skill"].body()
            == "Return `basic-skill-ok` when this skill is invoked."
        )

    mcp_results = asyncio.run(_exercise_channels(channels, mcp_enabled))
    print(
        json.dumps(
            {
                "skills_enabled": skills_enabled,
                "mcp_enabled": mcp_enabled,
                "baseline_skill": True,
                "plugin_skill": "basic-skill" in skills,
                **mcp_results,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
