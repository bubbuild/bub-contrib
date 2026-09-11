from __future__ import annotations

from pathlib import Path

import bub
from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict


@bub.config(name="acp-server")
class ACPServerSettings(bub.Settings):
    model_config = SettingsConfigDict(env_prefix="BUB_ACP_SERVER_", extra="ignore")

    channel_name: str = "acp-server"
    send_user_message_updates: bool = False
    context_window_size: int = Field(default=128_000, gt=0)
    host: str = "127.0.0.1"
    port: int = Field(default=28200, ge=1, le=65535)
    certfile: Path | None = None
    keyfile: Path | None = None

    @model_validator(mode="after")
    def validate_tls(self) -> ACPServerSettings:
        if (self.certfile is None) != (self.keyfile is None):
            raise ValueError("certfile and keyfile must be provided together")
        return self
