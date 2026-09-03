"""Exercise Agent Plugins through Bub's installed entry points in one process."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--skills-enabled", type=int, choices=(0, 1), required=True)
    parser.add_argument("--mcp-enabled", type=int, choices=(0, 1), required=True)
    return parser.parse_args()


def _write_runtime_files(
    *, plugin_root: Path, workspace: Path, skills_enabled: bool, mcp_enabled: bool
) -> Path:
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
                    "paths": [str(plugin_root)],
                    "auto_discover": False,
                    "skills_enabled": skills_enabled,
                    "mcp_enabled": mcp_enabled,
                    "data_root": str(bub_home / "agent-plugins"),
                }
            }
        ),
        encoding="utf-8",
    )
    return config_file


async def _call_tool(tool: Any, **arguments: str) -> str:
    result = tool.run(**arguments)
    if inspect.isawaitable(result):
        result = await result
    return str(result)


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
            assert channel._bootstrap_task is not None
            await asyncio.wait_for(asyncio.shield(channel._bootstrap_task), timeout=30)

        baseline_state = baseline.list()["baseline"]
        assert baseline_state.connected, baseline_state.error
        baseline_result = await _call_tool(
            REGISTRY["mcp.baseline_ping"], value="baseline"
        )
        assert baseline_result == "basic-mcp-ok:baseline"

        plugin_result = None
        if plugin_channel is not None:
            plugin_state = plugin_channel.list()["basic-agent-plugin.basic"]
            assert plugin_state.connected, plugin_state.error
            plugin_result = await _call_tool(
                REGISTRY["mcp.basic-agent-plugin.basic_ping"], value="plugin"
            )
            assert plugin_result == "basic-mcp-ok:plugin"

        return {
            "baseline_mcp": baseline_result,
            "plugin_mcp": plugin_result,
        }
    finally:
        for channel in reversed(active_channels):
            await channel.stop()


def main() -> None:
    args = _parse_args()
    plugin_root = args.plugin_root.resolve()
    workspace = args.workspace.resolve()
    skills_enabled = bool(args.skills_enabled)
    mcp_enabled = bool(args.mcp_enabled)
    workspace.mkdir(parents=True)
    config_file = _write_runtime_files(
        plugin_root=plugin_root,
        workspace=workspace,
        skills_enabled=skills_enabled,
        mcp_enabled=mcp_enabled,
    )
    os.chdir(workspace)

    from bub.framework import BubFramework
    from bub.skills import discover_skills

    framework = BubFramework(config_file=config_file)
    framework.load_hooks()
    assert framework._plugin_status["mcp"].is_success
    assert framework._plugin_status["agent-plugins"].is_success

    entry_points = {
        item.name: item.value for item in importlib.metadata.entry_points(group="bub")
    }
    assert entry_points["mcp"] == "bub_mcp.plugin:MCPPlugin"
    assert (
        entry_points["agent-plugins"] == "bub_agent_plugins.plugin:AgentPluginsPlugin"
    )

    async def message_handler(_message: Any) -> None:
        return None

    channels = framework.get_channels(message_handler)
    skills = {skill.name: skill for skill in discover_skills(workspace)}
    assert skills["baseline-skill"].body() == "Return `baseline-skill-ok`."
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
