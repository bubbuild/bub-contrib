from __future__ import annotations

import bub
from pydantic_settings import SettingsConfigDict


@bub.config(name="ag-ui")
class AGUISettings(bub.Settings):
    """Runtime settings for the AG-UI gateway channel."""

    model_config = SettingsConfigDict(env_prefix="BUB_AG_UI_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8088
    path: str = "/agent"
    health_path: str = "/agent/health"


def load_settings() -> AGUISettings:
    return bub.ensure_config(AGUISettings)
