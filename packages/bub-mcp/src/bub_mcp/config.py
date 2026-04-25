from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import bub
from pydantic import Field
from pydantic_settings import SettingsConfigDict


def default_config_path() -> Path:
    from bub.builtin.settings import load_settings

    return load_settings().home / "mcp.json"


def read_config(config_file: Path) -> dict[str, Any]:
    if not config_file.exists():
        return {}
    loaded = json.loads(config_file.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("MCP config file must contain a top-level mapping")
    return loaded.get("mcpServers", {})


@bub.config(name="mcp")
class MCPSettings(bub.Settings):
    model_config = SettingsConfigDict(env_prefix="BUB_MCP_", extra="ignore")

    config_path: Path = Field(default_factory=default_config_path)
    init_timeout_seconds: float | None = 20.0

    def read_mcp_servers(self) -> dict[str, Any]:
        return read_config(self.config_path)

    def write_mcp_servers(self, mcp_servers: dict[str, Any]) -> None:
        config = {"mcpServers": mcp_servers}
        self.config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
