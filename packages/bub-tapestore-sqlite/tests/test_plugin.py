from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import bub_tapestore_sqlite.plugin as plugin
from bub_tapestore_sqlite.plugin import SQLiteTapeStoreSettings
from bub_tapestore_sqlite.store import SQLiteTapeStore


def test_config_defaults_to_bub_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    monkeypatch.delenv("BUB_SQLITE_PATH", raising=False)

    config = SQLiteTapeStoreSettings()

    assert config.path is None
    assert config.embedding_model is None
    assert plugin._build_store(lambda: config)._path == tmp_path / "tapes.sqlite3"


def test_plugin_provides_singleton_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_SQLITE_PATH", str(tmp_path / "custom.sqlite3"))
    plugin._store.cache_clear()

    store = plugin.provide_tape_store()

    assert isinstance(store, SQLiteTapeStore)
    assert store is plugin.provide_tape_store()


def test_invalid_journal_mode_raises(monkeypatch) -> None:
    monkeypatch.setenv("BUB_SQLITE_JOURNAL_MODE", "INVALID")

    with pytest.raises(ValidationError, match="BUB_SQLITE_JOURNAL_MODE"):
        SQLiteTapeStoreSettings()


def test_negative_busy_timeout_raises(monkeypatch) -> None:
    monkeypatch.setenv("BUB_SQLITE_BUSY_TIMEOUT_MS", "-1")

    with pytest.raises(ValidationError, match="busy_timeout_ms"):
        SQLiteTapeStoreSettings()


def test_tape_store_from_env_returns_fresh_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_SQLITE_PATH", str(tmp_path / "fresh.sqlite3"))

    first = plugin.tape_store_from_env()
    second = plugin.tape_store_from_env()

    assert isinstance(first, SQLiteTapeStore)
    assert isinstance(second, SQLiteTapeStore)
    assert first is not second


def test_bub_home_from_bub_drives_default_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BUB_SQLITE_EMBEDDING_MODEL=openai:text-embedding-3-small\n",
        encoding="utf-8",
    )

    settings = SQLiteTapeStoreSettings(_env_file=env_file)

    assert settings.embedding_model == "openai:text-embedding-3-small"
    assert plugin._build_store(lambda: settings)._path == tmp_path / "tapes.sqlite3"


def test_onboard_config_collects_sqlite_settings(monkeypatch) -> None:
    text_answers = iter(
        [
            "/tmp/tapes.sqlite3",
            "openai:text-embedding-3-small",
            "7000",
            "WAL",
            "NORMAL",
        ]
    )
    monkeypatch.setattr(plugin.bub_inquirer, "ask_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        plugin.bub_inquirer,
        "ask_text",
        lambda *args, **kwargs: next(text_answers),
    )

    assert plugin.onboard_config({}) == {
        "tapestore-sqlite": {
            "path": "/tmp/tapes.sqlite3",
            "embedding_model": "openai:text-embedding-3-small",
            "busy_timeout_ms": "7000",
            "journal_mode": "WAL",
            "synchronous": "NORMAL",
        }
    }


def test_onboard_config_skips_sqlite_when_declined(monkeypatch) -> None:
    monkeypatch.setattr(plugin.bub_inquirer, "ask_confirm", lambda *args, **kwargs: False)

    assert plugin.onboard_config({}) is None
