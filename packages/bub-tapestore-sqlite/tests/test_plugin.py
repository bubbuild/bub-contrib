from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import bub_tapestore_sqlite.plugin as plugin
from bub_tapestore_sqlite.plugin import SQLiteTapeStoreSettings
from bub_tapestore_sqlite.store import SQLiteTapeStore


def test_config_defaults_to_bub_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    monkeypatch.delenv("BUB_TAPESTORE_SQLITE_PATH", raising=False)

    config = SQLiteTapeStoreSettings()

    assert config.bub_home == tmp_path
    assert config.path is None


def test_plugin_provides_singleton_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_TAPESTORE_SQLITE_PATH", str(tmp_path / "custom.sqlite3"))
    plugin._store.cache_clear()

    store = plugin.provide_tape_store()

    assert isinstance(store, SQLiteTapeStore)
    assert store is plugin.provide_tape_store()


def test_invalid_journal_mode_raises(monkeypatch) -> None:
    monkeypatch.setenv("BUB_TAPESTORE_SQLITE_JOURNAL_MODE", "INVALID")

    with pytest.raises(ValidationError, match="BUB_TAPESTORE_SQLITE_JOURNAL_MODE"):
        SQLiteTapeStoreSettings()


def test_negative_busy_timeout_raises(monkeypatch) -> None:
    monkeypatch.setenv("BUB_TAPESTORE_SQLITE_BUSY_TIMEOUT_MS", "-1")

    with pytest.raises(ValidationError, match="busy_timeout_ms"):
        SQLiteTapeStoreSettings()


def test_tape_store_from_env_returns_fresh_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_TAPESTORE_SQLITE_PATH", str(tmp_path / "fresh.sqlite3"))

    first = plugin.tape_store_from_env()
    second = plugin.tape_store_from_env()

    assert isinstance(first, SQLiteTapeStore)
    assert isinstance(second, SQLiteTapeStore)
    assert first is not second


def test_bub_home_from_env_file_drives_default_path(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("BUB_HOME=/tmp/from-env-file\n", encoding="utf-8")

    settings = SQLiteTapeStoreSettings(_env_file=env_file)

    assert settings.bub_home == Path("/tmp/from-env-file")
    assert plugin._build_store(lambda: settings)._path == Path(
        "/tmp/from-env-file/tapes.sqlite3"
    )
