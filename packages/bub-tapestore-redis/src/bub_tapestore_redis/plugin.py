from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

import redis.asyncio as redis
from bub import hookimpl
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub_tapestore_redis.store import DEFAULT_KEY_PREFIX, RedisTapeStore

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


class RedisTapeStoreSettings(BaseSettings):
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
    settings_factory: Callable[[], RedisTapeStoreSettings] = RedisTapeStoreSettings.from_env,
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
