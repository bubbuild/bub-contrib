from __future__ import annotations

import asyncio

import bub_tapestore_redis.plugin as plugin
from bub_tapestore_redis.plugin import RedisTapeStoreSettings
from bub_tapestore_redis.store import DEFAULT_KEY_PREFIX, RedisTapeStore


def test_settings_fall_back_to_defaults_when_env_values_are_blank(monkeypatch) -> None:
    monkeypatch.setenv("BUB_TAPESTORE_REDIS_URL", "  ")
    monkeypatch.setenv("BUB_TAPESTORE_REDIS_KEY_PREFIX", "  ")

    settings = RedisTapeStoreSettings.from_env()

    assert settings.resolved_url == "redis://localhost:6379/0"
    assert settings.resolved_key_prefix == DEFAULT_KEY_PREFIX


def test_settings_read_workspace_dotenv(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUB_TAPESTORE_REDIS_URL", raising=False)
    monkeypatch.delenv("BUB_TAPESTORE_REDIS_KEY_PREFIX", raising=False)
    (tmp_path / ".env").write_text(
        "BUB_TAPESTORE_REDIS_URL= redis://cache.example:6380/7 \n"
        "BUB_TAPESTORE_REDIS_KEY_PREFIX= bub:tapes \n",
        encoding="utf-8",
    )

    settings = RedisTapeStoreSettings.from_env()

    assert settings.resolved_url == "redis://cache.example:6380/7"
    assert settings.resolved_key_prefix == "bub:tapes"


def test_tape_store_from_env_returns_fresh_store(monkeypatch) -> None:
    monkeypatch.setenv("BUB_TAPESTORE_REDIS_URL", "redis://env.example:6379/2")
    monkeypatch.setenv("BUB_TAPESTORE_REDIS_KEY_PREFIX", "env:tapes")

    first = plugin.tape_store_from_env()
    second = plugin.tape_store_from_env()

    try:
        assert isinstance(first, RedisTapeStore)
        assert isinstance(second, RedisTapeStore)
        assert first is not second
        assert first._client.connection_pool.connection_kwargs["host"] == "env.example"
        assert first._client.connection_pool.connection_kwargs["db"] == 2
        assert first._keys.tapes == "{ZW52OnRhcGVz}:env:tapes:tapes"
    finally:
        asyncio.run(first._client.aclose())
        asyncio.run(second._client.aclose())


def test_plugin_provides_singleton_store(monkeypatch) -> None:
    plugin._store.cache_clear()
    monkeypatch.setenv("BUB_TAPESTORE_REDIS_URL", "redis://cached.example:6379/4")
    monkeypatch.setenv("BUB_TAPESTORE_REDIS_KEY_PREFIX", "cached:tapes")

    first = plugin.provide_tape_store()
    second = plugin.provide_tape_store()

    try:
        assert isinstance(first, RedisTapeStore)
        assert first is second
        assert first._client.connection_pool.connection_kwargs["host"] == "cached.example"
        assert first._client.connection_pool.connection_kwargs["db"] == 4
    finally:
        plugin._store.cache_clear()
        asyncio.run(first._client.aclose())
