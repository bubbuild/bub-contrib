from __future__ import annotations

from pathlib import Path

import bub_tapestore_sqlalchemy.plugin as plugin
import pytest
from bub_tapestore_sqlalchemy.plugin import SQLAlchemyTapeStoreSettings
from bub_tapestore_sqlalchemy.store import SQLAlchemyTapeStore


def test_config_defaults_to_bub_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    monkeypatch.delenv("BUB_TAPESTORE_SQLALCHEMY_URL", raising=False)

    settings = SQLAlchemyTapeStoreSettings.from_env()

    assert settings.resolved_url.startswith("sqlite+pysqlite:///")
    assert settings.resolved_url.endswith("/tapes.db")
    assert str(tmp_path) in settings.resolved_url


def test_config_reads_workspace_dotenv_without_bub_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "runtime-home"))
    monkeypatch.delenv("BUB_TAPESTORE_SQLALCHEMY_URL", raising=False)
    (tmp_path / ".env").write_text(
        "BUB_TAPESTORE_SQLALCHEMY_ECHO=true\n",
        encoding="utf-8",
    )

    settings = SQLAlchemyTapeStoreSettings.from_env()

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


def test_onboard_config_collects_sqlalchemy_settings(monkeypatch) -> None:
    monkeypatch.setattr(plugin.bub_inquirer, "ask_confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        plugin.bub_inquirer,
        "ask_text",
        lambda *args, **kwargs: "sqlite+pysqlite:////tmp/tapes.db",
    )

    assert plugin.onboard_config({}) == {
        "tapestore-sqlalchemy": {
            "url": "sqlite+pysqlite:////tmp/tapes.db",
            "echo": True,
        }
    }


def test_onboard_config_skips_sqlalchemy_when_declined(monkeypatch) -> None:
    monkeypatch.setattr(plugin.bub_inquirer, "ask_confirm", lambda *args, **kwargs: False)

    assert plugin.onboard_config({}) is None


def test_connect_args_default_empty(monkeypatch) -> None:
    monkeypatch.delenv("BUB_TAPESTORE_SQLALCHEMY_CONNECT_ARGS", raising=False)

    settings = SQLAlchemyTapeStoreSettings.from_env()

    assert settings.connect_args == {}


def test_connect_args_decoded_from_env_json(monkeypatch) -> None:
    monkeypatch.setenv(
        "BUB_TAPESTORE_SQLALCHEMY_CONNECT_ARGS",
        '{"auth_token": "abc", "sync_interval": 5}',
    )

    settings = SQLAlchemyTapeStoreSettings.from_env()

    assert settings.connect_args == {"auth_token": "abc", "sync_interval": 5}


def test_connect_args_invalid_json_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("BUB_TAPESTORE_SQLALCHEMY_CONNECT_ARGS", "not-json")

    with pytest.raises(Exception, match="JSON object"):
        SQLAlchemyTapeStoreSettings.from_env()


def test_connect_args_must_be_object(monkeypatch) -> None:
    monkeypatch.setenv("BUB_TAPESTORE_SQLALCHEMY_CONNECT_ARGS", "[1, 2, 3]")

    with pytest.raises(Exception, match="JSON object"):
        SQLAlchemyTapeStoreSettings.from_env()
