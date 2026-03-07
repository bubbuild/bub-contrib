from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from bub import hookimpl
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from bub_tapestore_sqlalchemy.store import SQLAlchemyTapeStore

DEFAULT_BUB_HOME = Path.home() / ".bub"

def _default_url(bub_home: Path) -> str:
    database_path = (bub_home.expanduser() / "tapes.db").resolve()
    return str(URL.create("sqlite+pysqlite", database=str(database_path)))


class SQLAlchemyTapeStoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bub_home: Path = Field(default=DEFAULT_BUB_HOME, validation_alias="BUB_HOME")
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

    @field_validator("bub_home", mode="before")
    @classmethod
    def _normalize_bub_home(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value.expanduser()
        return Path(str(value)).expanduser()

    @property
    def resolved_url(self) -> str:
        if self.url is None or not self.url.strip():
            return _default_url(self.bub_home)
        return self.url.strip()


def _build_store(
    settings_factory: Callable[[], SQLAlchemyTapeStoreSettings] = SQLAlchemyTapeStoreSettings.from_env,
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
