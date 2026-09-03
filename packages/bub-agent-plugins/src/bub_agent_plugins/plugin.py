"""Bub entry point for portable Agent Plugins."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import bub
from bub import hookimpl
from bub.channels import Channel
from bub.channels.contracts import MessageHandler
from bub.envelope import Envelope
from bub.turn import TurnState
from loguru import logger
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub_agent_plugins.loader import AgentPluginLoader
from bub_agent_plugins.mcp import create_agent_plugin_client
from bub_agent_plugins.models import AgentPluginLoadResult
from bub_agent_plugins.skills import PluginSkillRegistry

try:
    from bub_mcp.plugin import MCPChannel
except ModuleNotFoundError as exc:
    if exc.name == "bub_mcp":
        raise RuntimeError(
            "bub-agent-plugins requires bub-mcp>=0.2.0 in the Bub runtime environment"
        ) from exc
    raise

if not callable(getattr(MCPChannel, "from_server_configs", None)):
    raise RuntimeError(
        "bub-agent-plugins requires bub-mcp>=0.2.0 in the Bub runtime environment"
    )


def default_data_root() -> Path:
    return bub.home / "agent-plugins"


@bub.config(name="agent-plugins")
class AgentPluginsSettings(bub.Settings):
    model_config = SettingsConfigDict(env_prefix="BUB_AGENT_PLUGINS_", extra="ignore")

    paths: list[Path] = Field(default_factory=list)
    auto_discover: bool = True
    skills_enabled: bool = True
    mcp_enabled: bool = True
    data_root: Path = Field(default_factory=default_data_root)


class AgentPluginMCPChannel(MCPChannel):
    name = "agent-plugins.mcp"
    stop_when_all_failed = False

    def _create_client(self, server_name: str, server_config: dict[str, Any]) -> Any:
        return create_agent_plugin_client(
            server_name,
            server_config,
            self.settings.init_timeout_seconds,
        )


class AgentPluginsPlugin:
    def __init__(
        self,
        framework: Any,
        *,
        settings: AgentPluginsSettings | None = None,
    ) -> None:
        self.framework = framework
        self.settings = settings or bub.ensure_config(AgentPluginsSettings)
        self.catalog = AgentPluginLoadResult()
        self._skills = PluginSkillRegistry()
        self._mcp_channel: AgentPluginMCPChannel | None = None
        self._loaded_workspace: Path | None = None

    def load(self) -> AgentPluginLoadResult:
        workspace = Path(self.framework.workspace).resolve()
        if workspace == self._loaded_workspace:
            return self.catalog

        roots = _discover_plugin_roots(workspace, self.settings)
        loader = AgentPluginLoader(
            data_root=self.settings.data_root,
            skills_enabled=self.settings.skills_enabled,
            mcp_enabled=self.settings.mcp_enabled,
        )
        self.catalog = loader.load(roots)
        try:
            self._skills.activate(self.catalog.skill_directories)
        except OSError as exc:
            self._skills.close()
            self.catalog.diagnostics.append(
                f"skills component disabled: cannot register skill directories: {exc}"
            )
        self._mcp_channel = (
            AgentPluginMCPChannel.from_server_configs(self.catalog.mcp_servers)
            if self.settings.mcp_enabled and self.catalog.mcp_servers
            else None
        )
        self._loaded_workspace = workspace
        for diagnostic in self.catalog.diagnostics:
            logger.warning("bub-agent-plugins: {}", diagnostic)
        return self.catalog

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> TurnState:
        del message, session_id
        catalog = self.load()
        state: TurnState = {"agent_plugins": catalog}
        if self._mcp_channel is not None:
            state["agent_plugins_mcp"] = self._mcp_channel
        return state

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        del message_handler
        self.load()
        return [self._mcp_channel] if self._mcp_channel is not None else []


def _discover_plugin_roots(
    workspace: Path, settings: AgentPluginsSettings
) -> list[Path]:
    roots = [
        path if path.is_absolute() else workspace / path for path in settings.paths
    ]
    if not settings.auto_discover:
        return roots

    for parent in (
        workspace / ".agents" / "plugins",
        Path.home() / ".agents" / "plugins",
    ):
        if not parent.is_dir():
            continue
        roots.extend(
            child
            for child in sorted(parent.iterdir(), key=lambda path: path.name)
            if child.is_dir()
        )
    return roots
