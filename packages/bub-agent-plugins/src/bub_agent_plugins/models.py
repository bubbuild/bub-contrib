"""Validated Agent Plugin data passed between loading stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bub_agent_plugins.schemas import SCHEMA, AgentPluginSchema


@dataclass(frozen=True)
class AgentPluginManifest:
    name: str
    root: Path
    data: dict[str, Any]
    schema: AgentPluginSchema = SCHEMA


@dataclass
class LoadedAgentPlugin:
    manifest: AgentPluginManifest
    skill_directories: list[Path] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class AgentPluginLoadResult:
    plugins: list[LoadedAgentPlugin] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def skill_directories(self) -> list[Path]:
        return [
            skill_dir
            for plugin in self.plugins
            for skill_dir in plugin.skill_directories
        ]

    @property
    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        return {
            name: config
            for plugin in self.plugins
            for name, config in plugin.mcp_servers.items()
        }
