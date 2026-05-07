from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

import bub
import redis.asyncio as redis
from bub import hookimpl
from bub import inquirer as bub_inquirer
from pydantic import Field
from pydantic_settings import SettingsConfigDict

from bub_tapestore_redis.store import DEFAULT_KEY_PREFIX, RedisTapeStore

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
CONFIG_NAME = "tapestore-redis"


@bub.config(name=CONFIG_NAME)
class RedisTapeStoreSettings(bub.Settings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str = Field(
        default=DEFAULT_REDIS_URL,
        validation_alias="BUB_TAPESTORE_REDIS_URL",
    )
    key_prefix: str | None = Field(
        default=None,
        validation_alias="BUB_TAPESTORE_REDIS_KEY_PREFIX",
    )

    @classmethod
    def from_env(cls) -> RedisTapeStoreSettings:
        return cls()

    @property
    def resolved_url(self) -> str:
        return self.url.strip() or DEFAULT_REDIS_URL

    @property
    def resolved_key_prefix(self) -> str:
        if self.key_prefix is None or not self.key_prefix.strip():
            return DEFAULT_KEY_PREFIX
        return self.key_prefix.strip()


def _build_store(
    settings_factory: Callable[[], RedisTapeStoreSettings] = lambda: bub.ensure_config(
        RedisTapeStoreSettings
    ),
) -> RedisTapeStore:
    settings = settings_factory()
    client = redis.Redis.from_url(settings.resolved_url)
    return RedisTapeStore(client, key_prefix=settings.resolved_key_prefix)


@lru_cache(maxsize=1)
def _store() -> RedisTapeStore:
    return _build_store()


def tape_store_from_env() -> RedisTapeStore:
    return _build_store()


@hookimpl
def provide_tape_store() -> RedisTapeStore:
    return _store()


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    existing = current_config.get(CONFIG_NAME)
    configure = bub_inquirer.ask_confirm(
        "Configure Redis tape store",
        default=isinstance(existing, dict),
    )
    if not configure:
        return None

    current = existing if isinstance(existing, dict) else {}
    url_default = str(current.get("url") or DEFAULT_REDIS_URL)
    key_prefix_default = str(current.get("key_prefix") or DEFAULT_KEY_PREFIX)

    return {
        CONFIG_NAME: {
            "url": bub_inquirer.ask_text("Redis URL", default=url_default),
            "key_prefix": bub_inquirer.ask_text(
                "Redis key prefix",
                default=key_prefix_default,
            ),
        }
    }
