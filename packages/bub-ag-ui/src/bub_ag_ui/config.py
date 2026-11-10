from __future__ import annotations

import bub
from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict


@bub.config(name="ag-ui")
class AGUISettings(bub.Settings):
    model_config = SettingsConfigDict(env_prefix="BUB_AG_UI_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = Field(default=28201, ge=1, le=65535)
    token: SecretStr | None = None
    origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    max_body_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_run_seconds: float = Field(default=600, gt=0)
