from __future__ import annotations

import bub
from pydantic_settings import SettingsConfigDict


@bub.config(name="mcp-server")
class MCPServerSettings(bub.Settings):
    model_config = SettingsConfigDict(env_prefix="BUB_MCP_SERVER_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 28280
    path: str = "/sse"
    log_level: str = "info"
