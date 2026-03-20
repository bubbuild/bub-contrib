from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class QQConfig(BaseSettings):
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
    receive_mode: str = "webhook"
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 9009
    webhook_path: str = "/qq/webhook"
    webhook_callback_timeout_seconds: float = 15.0
    verify_signature: bool = True
    inbound_dedupe_size: int = 1024
    websocket_intents: int = 1 << 25
    websocket_use_shard_gateway: bool = False
    websocket_reconnect_delay_seconds: float = 5.0
