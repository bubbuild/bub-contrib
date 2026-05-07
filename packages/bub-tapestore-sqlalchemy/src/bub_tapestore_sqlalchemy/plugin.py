from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import bub
from bub import hookimpl
from bub import inquirer as bub_inquirer
from pydantic import Field
from pydantic_settings import SettingsConfigDict
from sqlalchemy import URL

from bub_tapestore_sqlalchemy.store import SQLAlchemyTapeStore

CONFIG_NAME = "tapestore-sqlalchemy"


def _default_url(bub_home: Path) -> str:
    database_path = (bub_home.expanduser() / "tapes.db").resolve()
    return str(URL.create("sqlite+pysqlite", database=str(database_path)))


@bub.config(name=CONFIG_NAME)
class SQLAlchemyTapeStoreSettings(bub.Settings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    url: str | None = Field(
        default=None,
        validation_alias="BUB_TAPESTORE_SQLALCHEMY_URL",
    )
    echo: bool = Field(
        default=False,
        validation_alias="BUB_TAPESTORE_SQLALCHEMY_ECHO",
    )

    @classmethod
    def from_env(cls) -> SQLAlchemyTapeStoreSettings:
        return cls()

    @property
    def resolved_url(self) -> str:
        if self.url is None or not self.url.strip():
            return _default_url(Path(bub.home).expanduser())
        return self.url.strip()


def _build_store(
    settings_factory: Callable[[], SQLAlchemyTapeStoreSettings] = lambda: bub.ensure_config(
        SQLAlchemyTapeStoreSettings
    ),
) -> SQLAlchemyTapeStore:
    settings = settings_factory()
    return SQLAlchemyTapeStore(url=settings.resolved_url, echo=settings.echo)


@lru_cache(maxsize=1)
def _store() -> SQLAlchemyTapeStore:
    return _build_store()


def tape_store_from_env() -> SQLAlchemyTapeStore:
    return _build_store()


@hookimpl
def provide_tape_store() -> SQLAlchemyTapeStore:
    return _store()


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    existing = current_config.get(CONFIG_NAME)
    configure = bub_inquirer.ask_confirm(
        "Configure SQLAlchemy tape store",
        default=isinstance(existing, dict),
    )
    if not configure:
        return None

    current = existing if isinstance(existing, dict) else {}
    url = bub_inquirer.ask_text(
        "SQLAlchemy tape store URL (optional)",
        default=str(current.get("url") or ""),
    )
    echo = bub_inquirer.ask_confirm(
        "SQLAlchemy echo SQL",
        default=bool(current.get("echo")),
    )

    config: dict[str, Any] = {"echo": echo}
    if url:
        config["url"] = url
    return {CONFIG_NAME: config}
