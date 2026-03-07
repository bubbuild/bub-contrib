from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from republic import TapeEntry, TapeQuery
from republic.core.errors import ErrorKind
from republic.core.results import ErrorPayload

ALLOWED_JOURNAL_MODES = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
ALLOWED_SYNCHRONOUS_MODES = {"OFF", "NORMAL", "FULL", "EXTRA"}


def _normalize_mode(name: str, value: str, allowed: set[str]) -> str:
    normalized = value.strip().upper()
    if normalized not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")
    return normalized


def normalize_journal_mode(value: str) -> str:
    return _normalize_mode(
        "BUB_TAPESTORE_SQLITE_JOURNAL_MODE",
        value,
        ALLOWED_JOURNAL_MODES,
    )


def normalize_synchronous_mode(value: str) -> str:
    return _normalize_mode(
        "BUB_TAPESTORE_SQLITE_SYNCHRONOUS",
        value,
        ALLOWED_SYNCHRONOUS_MODES,
    )


class SQLiteTapeStore:
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        journal_mode: str = "WAL",
        synchronous: str = "NORMAL",
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if busy_timeout_ms < 0:
            raise ValueError("BUB_TAPESTORE_SQLITE_BUSY_TIMEOUT_MS must be >= 0")
        journal_mode = normalize_journal_mode(journal_mode)
        synchronous = normalize_synchronous_mode(synchronous)
        self._connection = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._configure(
                busy_timeout_ms=busy_timeout_ms,
                journal_mode=journal_mode,
                synchronous=synchronous,
            )
            self._initialize_schema()

    def list_tapes(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT name FROM tapes ORDER BY name"
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def reset(self, tape: str) -> None:
        with self._lock:
            with self._write_transaction():
                self._connection.execute("DELETE FROM tapes WHERE name = ?", (tape,))

    def append(self, tape: str, entry: TapeEntry) -> None:
        payload_json = json.dumps(dict(entry.payload), ensure_ascii=False)
        meta_json = json.dumps(dict(entry.meta), ensure_ascii=False)
        anchor_name = self._anchor_name_of(entry)

        with self._lock:
            with self._write_transaction():
                self._connection.execute(
                    "INSERT INTO tapes (name) VALUES (?) ON CONFLICT(name) DO NOTHING",
                    (tape,),
                )
                tape_id, next_id = self._next_entry_identity(tape)
                self._connection.execute(
                    """
                    INSERT INTO tape_entries (
                        tape_id,
                        entry_id,
                        kind,
                        anchor_name,
                        payload_json,
                        meta_json,
                        entry_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tape_id,
                        next_id,
                        entry.kind,
                        anchor_name,
                        payload_json,
                        meta_json,
                        entry.date,
                    ),
                )
                self._connection.execute(
                    "UPDATE tapes SET last_entry_id = ? WHERE id = ?",
                    (next_id, tape_id),
                )

    def fetch_all(self, query: TapeQuery[SQLiteTapeStore]) -> Iterable[TapeEntry]:
        with self._lock:
            tape_id = self._tape_id(query.tape)
            if tape_id is None:
                self._raise_missing_for_query(query)
                return []

            lower_bound, upper_bound = self._resolve_bounds(
                tape_id=tape_id, query=query
            )
            sql = [
                """
                SELECT entry_id, kind, payload_json, meta_json, entry_date
                FROM tape_entries
                WHERE tape_id = ?
                """
            ]
            params: list[Any] = [tape_id]

            if lower_bound is not None:
                sql.append("AND entry_id > ?")
                params.append(lower_bound)
            if upper_bound is not None:
                sql.append("AND entry_id < ?")
                params.append(upper_bound)
            if query._kinds:
                placeholders = ", ".join("?" for _ in query._kinds)
                sql.append(f"AND kind IN ({placeholders})")
                params.extend(query._kinds)

            sql.append("ORDER BY entry_id")
            if query._limit is not None:
                sql.append("LIMIT ?")
                params.append(query._limit)

            rows = self._connection.execute("\n".join(sql), params).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def _configure(
        self, *, busy_timeout_ms: int, journal_mode: str, synchronous: str
    ) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA journal_mode = {journal_mode}")
        self._connection.execute(f"PRAGMA synchronous = {synchronous}")
        self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")

    def _initialize_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tapes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                last_entry_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tape_entries (
                tape_id INTEGER NOT NULL REFERENCES tapes(id) ON DELETE CASCADE,
                entry_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                anchor_name TEXT,
                payload_json TEXT NOT NULL,
                meta_json TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tape_id, entry_id)
            );

            CREATE INDEX IF NOT EXISTS idx_tape_entries_kind
                ON tape_entries (tape_id, kind, entry_id);

            CREATE INDEX IF NOT EXISTS idx_tape_entries_anchor_name
                ON tape_entries (tape_id, anchor_name, entry_id)
                WHERE kind = 'anchor';
            """
        )

    def _tape_id(self, tape: str) -> int | None:
        row = self._connection.execute(
            "SELECT id FROM tapes WHERE name = ?",
            (tape,),
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def _next_entry_identity(self, tape: str) -> tuple[int, int]:
        row = self._connection.execute(
            "SELECT id, last_entry_id FROM tapes WHERE name = ?",
            (tape,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Failed to resolve tape row for {tape!r}.")
        return int(row["id"]), int(row["last_entry_id"]) + 1

    @contextmanager
    def _write_transaction(self) -> Iterable[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _resolve_bounds(
        self,
        *,
        tape_id: int,
        query: TapeQuery[SQLiteTapeStore],
    ) -> tuple[int | None, int | None]:
        if query._between is not None:
            start_name, end_name = query._between
            start_id = self._find_anchor_id(
                tape_id=tape_id, name=start_name, forward=False
            )
            if start_id is None:
                raise ErrorPayload(
                    ErrorKind.NOT_FOUND, f"Anchor '{start_name}' was not found."
                )
            end_id = self._find_anchor_id(
                tape_id=tape_id, name=end_name, forward=True, after_entry_id=start_id
            )
            if end_id is None:
                raise ErrorPayload(
                    ErrorKind.NOT_FOUND, f"Anchor '{end_name}' was not found."
                )
            return start_id, end_id

        if query._after_last:
            anchor_id = self._find_anchor_id(tape_id=tape_id, name=None, forward=False)
            if anchor_id is None:
                raise ErrorPayload(ErrorKind.NOT_FOUND, "No anchors found in tape.")
            return anchor_id, None

        if query._after_anchor is not None:
            anchor_id = self._find_anchor_id(
                tape_id=tape_id, name=query._after_anchor, forward=False
            )
            if anchor_id is None:
                raise ErrorPayload(
                    ErrorKind.NOT_FOUND,
                    f"Anchor '{query._after_anchor}' was not found.",
                )
            return anchor_id, None

        return None, None

    def _find_anchor_id(
        self,
        *,
        tape_id: int,
        name: str | None,
        forward: bool,
        after_entry_id: int = 0,
    ) -> int | None:
        direction = "ASC" if forward else "DESC"
        sql = [
            """
            SELECT entry_id
            FROM tape_entries
            WHERE tape_id = ?
              AND kind = 'anchor'
            """
        ]
        params: list[Any] = [tape_id]

        if name is not None:
            sql.append("AND anchor_name = ?")
            params.append(name)
        if after_entry_id > 0:
            sql.append("AND entry_id > ?")
            params.append(after_entry_id)

        sql.append(f"ORDER BY entry_id {direction}")
        sql.append("LIMIT 1")
        row = self._connection.execute("\n".join(sql), params).fetchone()
        if row is None:
            return None
        return int(row["entry_id"])

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> TapeEntry:
        payload = json.loads(str(row["payload_json"]))
        meta = json.loads(str(row["meta_json"]))
        if not isinstance(payload, dict):
            payload = {}
        if not isinstance(meta, dict):
            meta = {}
        return TapeEntry(
            id=int(row["entry_id"]),
            kind=str(row["kind"]),
            payload=dict(payload),
            meta=dict(meta),
            date=str(row["entry_date"]),
        )

    @staticmethod
    def entry_from_payload(payload: object) -> TapeEntry | None:
        if not isinstance(payload, dict):
            return None
        entry_id = payload.get("id")
        kind = payload.get("kind")
        entry_payload = payload.get("payload")
        meta = payload.get("meta")
        date = payload.get("date")
        if not isinstance(entry_id, int):
            return None
        if not isinstance(kind, str):
            return None
        if not isinstance(entry_payload, dict):
            return None
        if not isinstance(meta, dict):
            meta = {}
        if not isinstance(date, str):
            return None
        return TapeEntry(
            id=entry_id,
            kind=kind,
            payload=dict(entry_payload),
            meta=dict(meta),
            date=date,
        )

    @staticmethod
    def _anchor_name_of(entry: TapeEntry) -> str | None:
        if entry.kind != "anchor":
            return None
        name = entry.payload.get("name")
        if isinstance(name, str) and name:
            return name
        return None

    @staticmethod
    def _raise_missing_for_query(query: TapeQuery[SQLiteTapeStore]) -> None:
        if query._between is not None:
            start_name, _ = query._between
            raise ErrorPayload(
                ErrorKind.NOT_FOUND, f"Anchor '{start_name}' was not found."
            )
        if query._after_last:
            raise ErrorPayload(ErrorKind.NOT_FOUND, "No anchors found in tape.")
        if query._after_anchor is not None:
            raise ErrorPayload(
                ErrorKind.NOT_FOUND, f"Anchor '{query._after_anchor}' was not found."
            )
