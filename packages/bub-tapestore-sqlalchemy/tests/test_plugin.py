from __future__ import annotations

from pathlib import Path

import bub_tapestore_sqlalchemy.plugin as plugin
from bub_tapestore_sqlalchemy.plugin import SQLAlchemyTapeStoreSettings
from bub_tapestore_sqlalchemy.store import SQLAlchemyTapeStore


def test_config_defaults_to_bub_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    monkeypatch.delenv("BUB_TAPESTORE_SQLALCHEMY_URL", raising=False)

    settings = SQLAlchemyTapeStoreSettings.from_env()

    assert settings.bub_home == tmp_path
    assert settings.resolved_url.startswith("sqlite+pysqlite:///")
    assert settings.resolved_url.endswith("/tapes.db")


def test_config_reads_workspace_dotenv(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUB_HOME", raising=False)
    monkeypatch.delenv("BUB_TAPESTORE_SQLALCHEMY_URL", raising=False)
    (tmp_path / ".env").write_text(
        "BUB_HOME=./runtime-home\nBUB_TAPESTORE_SQLALCHEMY_ECHO=true\n",
        encoding="utf-8",
    )

    settings = SQLAlchemyTapeStoreSettings.from_env()

    assert settings.bub_home == Path("runtime-home")
    assert settings.echo is True
    assert settings.resolved_url.endswith("/runtime-home/tapes.db")


def test_plugin_provides_singleton_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "BUB_TAPESTORE_SQLALCHEMY_URL",
        f"sqlite+pysqlite:///{tmp_path / 'custom.db'}",
    )
    plugin._store.cache_clear()

    store = plugin.provide_tape_store()

    assert isinstance(store, SQLAlchemyTapeStore)
    assert store is plugin.provide_tape_store()


def test_tape_store_from_env_returns_fresh_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "BUB_TAPESTORE_SQLALCHEMY_URL",
        f"sqlite+pysqlite:///{tmp_path / 'fresh.db'}",
    )

    first = plugin.tape_store_from_env()
    second = plugin.tape_store_from_env()

    assert isinstance(first, SQLAlchemyTapeStore)
    assert isinstance(second, SQLAlchemyTapeStore)
    assert first is not second


def test_tape_store_from_env_creates_default_sqlite_parent_directory(
    monkeypatch, tmp_path: Path
) -> None:
    bub_home = tmp_path / "nested" / "runtime-home"
    monkeypatch.setenv("BUB_HOME", str(bub_home))
    monkeypatch.delenv("BUB_TAPESTORE_SQLALCHEMY_URL", raising=False)

    store = plugin.tape_store_from_env()

    assert isinstance(store, SQLAlchemyTapeStore)
    assert bub_home.exists()
    assert (bub_home / "tapes.db").exists()
