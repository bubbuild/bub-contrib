from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from bub import hookimpl
from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub_tapestore_sqlite.store import (
    SQLiteTapeStore,
    normalize_journal_mode,
    normalize_synchronous_mode,
)

DEFAULT_BUB_HOME = Path.home() / ".bub"


class SQLiteTapeStoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BUB_TAPESTORE_SQLITE_",
        env_file=".env",
        extra="ignore",
    )

    bub_home: Path = Field(
        default=DEFAULT_BUB_HOME,
        validation_alias=AliasChoices("BUB_HOME", "bub_home"),
    )
    path: Path | None = None
    busy_timeout_ms: int = Field(default=5000, ge=0)
    journal_mode: str = "WAL"
    synchronous: str = "NORMAL"

    @field_validator("bub_home", mode="after")
    @classmethod
    def _normalize_bub_home(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("path", mode="after")
    @classmethod
    def _normalize_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser()

    @field_validator("journal_mode", mode="after")
    @classmethod
    def _normalize_journal_mode(cls, value: str) -> str:
        return normalize_journal_mode(value)

    @field_validator("synchronous", mode="after")
    @classmethod
    def _normalize_synchronous(cls, value: str) -> str:
        return normalize_synchronous_mode(value)


def _build_store(
    settings_factory: Callable[[], SQLiteTapeStoreSettings] = SQLiteTapeStoreSettings,
) -> SQLiteTapeStore:
    settings = settings_factory()
    path = settings.path or settings.bub_home / "tapes.sqlite3"
    return SQLiteTapeStore(
        path=path,
        busy_timeout_ms=settings.busy_timeout_ms,
        journal_mode=settings.journal_mode,
        synchronous=settings.synchronous,
    )


@lru_cache(maxsize=1)
def _store() -> SQLiteTapeStore:
    return _build_store()


def tape_store_from_env() -> SQLiteTapeStore:
    return _build_store()


@hookimpl
def provide_tape_store() -> SQLiteTapeStore:
    return _store()
