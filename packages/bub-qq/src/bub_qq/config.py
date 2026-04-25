from __future__ import annotations

import bub
from pydantic import Field
from pydantic_settings import SettingsConfigDict


@bub.config(name="qq")
class QQConfig(bub.Settings):
    """QQ Open Platform adapter config."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_QQ_",
        env_file=".env",
        extra="ignore",
    )

    appid: str = ""
    secret: str = ""
    token_url: str = "https://bots.qq.com/app/getAppAccessToken"
    openapi_base_url: str = "https://api.sgroup.qq.com"
    timeout_seconds: float = 30.0
    token_refresh_skew_seconds: int = 60
    receive_mode: str = Field(
        ...,
        description="QQ inbound transport mode. Must be set to 'webhook' or 'websocket'.",
    )
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    webhook_path: str = "/qq/webhook"
    webhook_callback_timeout_seconds: float = 15.0
    verify_signature: bool = True
    inbound_dedupe_size: int = Field(default=1024, ge=1)
    websocket_intents: int = 1 << 25
    websocket_use_shard_gateway: bool = False
    websocket_reconnect_delay_seconds: float = 5.0
