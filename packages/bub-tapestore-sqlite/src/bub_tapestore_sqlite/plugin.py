from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import bub
from bub import hookimpl
from bub import inquirer as bub_inquirer
from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from bub_tapestore_sqlite.store import (
    SQLiteTapeStore,
    normalize_journal_mode,
    normalize_synchronous_mode,
)

CONFIG_NAME = "tapestore-sqlite"


@bub.config(name=CONFIG_NAME)
class SQLiteTapeStoreSettings(bub.Settings):
    model_config = SettingsConfigDict(
        env_prefix="BUB_SQLITE_",
        env_file=".env",
        extra="ignore",
    )

    path: Path | None = None
    busy_timeout_ms: int = Field(default=5000, ge=0)
    journal_mode: str = "WAL"
    synchronous: str = "NORMAL"
    embedding_model: str | None = None

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
    settings_factory: Callable[[], SQLiteTapeStoreSettings] = lambda: bub.ensure_config(
        SQLiteTapeStoreSettings
    ),
) -> SQLiteTapeStore:
    settings = settings_factory()
    path = settings.path or Path(bub.home).expanduser() / "tapes.sqlite3"
    return SQLiteTapeStore(
        path=path,
        busy_timeout_ms=settings.busy_timeout_ms,
        journal_mode=settings.journal_mode,
        synchronous=settings.synchronous,
        embedding_model=settings.embedding_model,
    )


@lru_cache(maxsize=1)
def _store() -> SQLiteTapeStore:
    return _build_store()


def tape_store_from_env() -> SQLiteTapeStore:
    return _build_store()


@hookimpl
def provide_tape_store() -> SQLiteTapeStore:
    return _store()


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    existing = current_config.get(CONFIG_NAME)
    configure = bub_inquirer.ask_confirm(
        "Configure SQLite tape store",
        default=isinstance(existing, dict),
    )
    if not configure:
        return None

    current = existing if isinstance(existing, dict) else {}
    path = bub_inquirer.ask_text(
        "SQLite tape store path (optional)",
        default=str(current.get("path") or ""),
    )
    embedding_model = bub_inquirer.ask_text(
        "SQLite embedding model (optional)",
        default=str(current.get("embedding_model") or ""),
    )

    config: dict[str, Any] = {
        "busy_timeout_ms": bub_inquirer.ask_text(
            "SQLite busy timeout ms",
            default=str(current.get("busy_timeout_ms") or "5000"),
        ),
        "journal_mode": bub_inquirer.ask_text(
            "SQLite journal mode",
            default=str(current.get("journal_mode") or "WAL"),
        ),
        "synchronous": bub_inquirer.ask_text(
            "SQLite synchronous mode",
            default=str(current.get("synchronous") or "NORMAL"),
        ),
    }
    if path:
        config["path"] = path
    if embedding_model:
        config["embedding_model"] = embedding_model
    return {CONFIG_NAME: config}
