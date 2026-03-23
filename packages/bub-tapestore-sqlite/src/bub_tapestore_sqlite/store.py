from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite
import sqlite_vec
from any_llm import AnyLLM
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
        "BUB_SQLITE_JOURNAL_MODE",
        value,
        ALLOWED_JOURNAL_MODES,
    )


def normalize_synchronous_mode(value: str) -> str:
    return _normalize_mode(
        "BUB_SQLITE_SYNCHRONOUS",
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
        embedding_model: str | None = None,
    ) -> None:
        from bub.builtin.agent import _build_llm
        from bub.builtin.settings import AgentSettings
        from republic.tape.context import TapeContext

        self._path = Path(path)
        self._llm = _build_llm(AgentSettings(), self, TapeContext())  # type: ignore[arg-type]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._busy_timeout_ms = busy_timeout_ms
        self._journal_mode = normalize_journal_mode(journal_mode)
        self._synchronous = normalize_synchronous_mode(synchronous)
        self._embedding_model = embedding_model
        self._vector_dimensions: int | None = None
        self._connection: aiosqlite.Connection | None = None
        self._operation_lock: asyncio.Lock | None = None

    async def list_tapes(self) -> list[str]:
        async with self._lock():
            connection = await self._ensure_connection()
            rows = await self._fetchall(
                connection,
                "SELECT name FROM tapes ORDER BY name",
            )
        return [str(row["name"]) for row in rows]

    async def reset(self, tape: str) -> None:
        async with self._lock():
            connection = await self._ensure_connection()
            await self._begin_immediate(connection)
            try:
                tape_id = await self._tape_id(connection, tape)
                if tape_id is not None:
                    await self._delete_vectors_for_tape(connection, tape_id)
                await connection.execute("DELETE FROM tapes WHERE name = ?", (tape,))
            except Exception:
                await connection.rollback()
                raise
            await connection.commit()

    async def append(self, tape: str, entry: TapeEntry) -> None:
        payload_json = json.dumps(dict(entry.payload), ensure_ascii=False)
        meta_json = json.dumps(dict(entry.meta), ensure_ascii=False)
        anchor_name = self._anchor_name_of(entry)
        text_value = self._text_of(entry)

        async with self._lock():
            connection = await self._ensure_connection()
            await self._begin_immediate(connection)
            try:
                await connection.execute(
                    "INSERT INTO tapes (name) VALUES (?) ON CONFLICT(name) DO NOTHING",
                    (tape,),
                )
                tape_id = await self._require_tape_id(connection, tape)
                cursor = await connection.execute(
                    """
                    INSERT INTO tape_entries (
                        tape_id,
                        kind,
                        text,
                        anchor_name,
                        payload_json,
                        meta_json,
                        entry_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tape_id,
                        entry.kind,
                        text_value,
                        anchor_name,
                        payload_json,
                        meta_json,
                        entry.date,
                    ),
                )
                try:
                    entry_id = cursor.lastrowid
                finally:
                    await cursor.close()
                if entry_id is None:
                    raise RuntimeError("Failed to allocate tape entry id.")
            except Exception:
                await connection.rollback()
                raise
            await connection.commit()

    async def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]:
        async with self._lock():
            connection = await self._ensure_connection()
            tape_id = await self._tape_id(connection, query.tape)
            if tape_id is None:
                self._raise_missing_for_query(query)
                return []

            if query._query:
                if self._embedding_model:
                    return await self._fetch_by_semantic_query(
                        connection=connection,
                        tape_id=tape_id,
                        query=query,
                    )

            lower_bound, upper_bound = await self._resolve_bounds(
                connection=connection,
                tape_id=tape_id,
                query=query,
            )
            sql = [
                """
                SELECT id AS entry_id, kind, payload_json, meta_json, entry_date
                FROM tape_entries
                WHERE tape_id = ?
                """
            ]
            params: list[Any] = [tape_id]

            if query._query:
                sql.append("AND text IS NOT NULL")
                sql.append("AND LOWER(text) LIKE LOWER(?)")
                params.append(f"%{query._query}%")
            if lower_bound is not None:
                sql.append("AND id > ?")
                params.append(lower_bound)
            if upper_bound is not None:
                sql.append("AND id < ?")
                params.append(upper_bound)
            if query._kinds:
                placeholders = ", ".join("?" for _ in query._kinds)
                sql.append(f"AND kind IN ({placeholders})")
                params.extend(query._kinds)
            if query._between_dates is not None:
                start_date, end_date = query._between_dates
                sql.append("AND entry_date >= ?")
                sql.append("AND entry_date <= ?")
                params.extend([start_date, end_date])

            sql.append("ORDER BY id")
            if query._limit is not None:
                sql.append("LIMIT ?")
                params.append(query._limit)

            rows = await self._fetchall(connection, "\n".join(sql), params)
        return [self._entry_from_row(row) for row in rows]

    async def _compute_embedding(self, texts: list[str]) -> list[list[float]]:
        if self._embedding_model is None:
            raise RuntimeError("No embedding model configured for tape store.")
        provider, model = self._embedding_model.split(":", 1)
        llm: AnyLLM = self._llm._core.get_client(provider)
        response = await llm.aembedding(model, texts)
        return self._embedding_response_to_vectors(response)

    async def _fetch_by_semantic_query(
        self,
        *,
        connection: aiosqlite.Connection,
        tape_id: int,
        query: TapeQuery,
    ) -> list[TapeEntry]:
        lower_bound, upper_bound = await self._resolve_bounds(
            connection=connection,
            tape_id=tape_id,
            query=query,
        )
        await self._embed_unindexed_messages(connection, tape_id)
        if self._vector_dimensions is None:
            return []

        embeddings = await self._compute_embedding([query._query])
        if len(embeddings) != 1:
            raise RuntimeError("Semantic query must produce exactly one embedding.")

        sql = [
            """
            SELECT
                te.id AS entry_id,
                te.kind,
                te.payload_json,
                te.meta_json,
                te.entry_date
            FROM tape_entry_vectors AS tev
            JOIN tape_entry_vector_index AS tvi
              ON tvi.id = tev.rowid
            JOIN tape_entries AS te
              ON te.tape_id = tvi.tape_id
             AND te.id = tvi.entry_id
            WHERE tev.embedding MATCH ?
              AND k = ?
              AND tvi.tape_id = ?
            """
        ]
        params: list[Any] = [
            sqlite_vec.serialize_float32(
                self._normalize_embedding(embeddings[0], self._vector_dimensions)
            ),
            query._limit or 10,
            tape_id,
        ]
        if lower_bound is not None:
            sql.append("AND te.id > ?")
            params.append(lower_bound)
        if upper_bound is not None:
            sql.append("AND te.id < ?")
            params.append(upper_bound)
        if query._between_dates is not None:
            start_date, end_date = query._between_dates
            sql.append("AND te.entry_date >= ?")
            sql.append("AND te.entry_date <= ?")
            params.extend([start_date, end_date])
        if query._kinds:
            placeholders = ", ".join("?" for _ in query._kinds)
            sql.append(f"AND tvi.kind IN ({placeholders})")
            params.extend(query._kinds)

        sql.append("ORDER BY tev.distance, te.id")
        rows = await self._fetchall(connection, "\n".join(sql), params)
        return [self._entry_from_row(row) for row in rows]

    async def _embed_unindexed_messages(
        self,
        connection: aiosqlite.Connection,
        tape_id: int,
    ) -> int:
        rows = await self._fetchall(
            connection,
            """
            SELECT te.id, te.kind, te.text, te.payload_json
            FROM tape_entries AS te
            LEFT JOIN tape_entry_vector_index AS tvi
              ON tvi.tape_id = te.tape_id
             AND tvi.entry_id = te.id
            WHERE te.tape_id = ?
              AND te.kind = 'message'
              AND tvi.id IS NULL
            ORDER BY te.id
            """,
            [tape_id],
        )
        if not rows:
            return 0

        embeddings = await self._compute_embedding([row["text"] for row in rows])

        if len(embeddings) != len(rows):
            raise RuntimeError("Embedding response count did not match requested rows.")

        await self._begin_immediate(connection)
        indexed_count = 0
        try:
            await self._ensure_vector_table(connection, len(embeddings[0]))
            for row, embedding in zip(rows, embeddings, strict=True):
                await self._index_entry_vector(
                    connection,
                    tape_id=tape_id,
                    entry_id=int(row["id"]),
                    kind=str(row["kind"]),
                    embedding=embedding,
                )
                indexed_count += 1
        except Exception:
            await connection.rollback()
            raise
        await connection.commit()
        return indexed_count

    async def close(self) -> None:
        async with self._lock():
            if self._connection is None:
                return
            await self._connection.close()
            self._connection = None

    def _lock(self) -> asyncio.Lock:
        if self._operation_lock is None:
            self._operation_lock = asyncio.Lock()
        return self._operation_lock

    async def _ensure_connection(self) -> aiosqlite.Connection:
        if self._connection is not None:
            return self._connection

        connection = await aiosqlite.connect(self._path, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        await self._configure(connection)
        await self._initialize_schema(connection)
        await self._initialize_vector_search(connection)
        self._connection = connection
        return connection

    async def _configure(self, connection: aiosqlite.Connection) -> None:
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute(f"PRAGMA journal_mode = {self._journal_mode}")
        await connection.execute(f"PRAGMA synchronous = {self._synchronous}")
        await connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")

    async def _initialize_schema(self, connection: aiosqlite.Connection) -> None:
        await connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tapes (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tape_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tape_id INTEGER NOT NULL REFERENCES tapes(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                text TEXT,
                anchor_name TEXT,
                payload_json TEXT NOT NULL,
                meta_json TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS tape_store_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tape_entry_vector_index (
                id INTEGER PRIMARY KEY,
                tape_id INTEGER NOT NULL REFERENCES tapes(id) ON DELETE CASCADE,
                entry_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tape_id, entry_id)
            );
            """
        )
        await self._ensure_tape_entry_indexes(connection)
        await self._ensure_vector_indexes(connection)

    async def _initialize_vector_search(self, connection: aiosqlite.Connection) -> None:
        self._vector_dimensions = await self._load_vector_dimensions(connection)
        if self._embedding_model is None and self._vector_dimensions is None:
            return

        await connection.enable_load_extension(True)
        try:
            await connection.load_extension(sqlite_vec.loadable_path())
        finally:
            await connection.enable_load_extension(False)

        if self._vector_dimensions is not None:
            await self._ensure_vector_table(connection, self._vector_dimensions)

    async def _ensure_tape_entry_indexes(
        self, connection: aiosqlite.Connection
    ) -> None:
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tape_entries_kind
                ON tape_entries (tape_id, kind, id)
            """
        )
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tape_entries_anchor_name
                ON tape_entries (tape_id, anchor_name, id)
                WHERE kind = 'anchor'
            """
        )
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tape_entries_text
                ON tape_entries (tape_id, text, id)
                WHERE text IS NOT NULL
            """
        )

    async def _ensure_vector_indexes(self, connection: aiosqlite.Connection) -> None:
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tape_entry_vector_index_lookup
                ON tape_entry_vector_index (tape_id, kind, entry_id)
            """
        )

    async def _load_vector_dimensions(
        self, connection: aiosqlite.Connection
    ) -> int | None:
        row = await self._fetchone(
            connection,
            "SELECT value FROM tape_store_metadata WHERE key = 'vector_dimensions'",
        )
        if row is None:
            return None
        return int(row["value"])

    async def _ensure_vector_table(
        self,
        connection: aiosqlite.Connection,
        dimensions: int,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("vector dimensions must be >= 1")
        if (
            self._vector_dimensions is not None
            and self._vector_dimensions != dimensions
        ):
            raise RuntimeError(
                f"Embedding dimensions {dimensions} do not match stored size {self._vector_dimensions}."
            )
        self._vector_dimensions = dimensions
        await connection.execute(
            """
            INSERT INTO tape_store_metadata (key, value)
            VALUES ('vector_dimensions', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(dimensions),),
        )
        await connection.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS tape_entry_vectors
            USING vec0(embedding float[{dimensions}])
            """
        )

    async def _tape_id(self, connection: aiosqlite.Connection, tape: str) -> int | None:
        row = await self._fetchone(
            connection,
            "SELECT id FROM tapes WHERE name = ?",
            (tape,),
        )
        if row is None:
            return None
        return int(row["id"])

    async def _require_tape_id(
        self, connection: aiosqlite.Connection, tape: str
    ) -> int:
        tape_id = await self._tape_id(connection, tape)
        if tape_id is None:
            raise RuntimeError(f"Failed to resolve tape row for {tape!r}.")
        return tape_id

    async def _delete_vectors_for_tape(
        self,
        connection: aiosqlite.Connection,
        tape_id: int,
    ) -> None:
        if self._vector_dimensions is None:
            return
        await connection.execute(
            """
            DELETE FROM tape_entry_vectors
            WHERE rowid IN (
            SELECT id
            FROM tape_entry_vector_index
            WHERE tape_id = ?
            )
            """,
            (tape_id,),
        )
        await connection.execute(
            "DELETE FROM tape_entry_vector_index WHERE tape_id = ?",
            (tape_id,),
        )

    async def _index_entry_vector(
        self,
        connection: aiosqlite.Connection,
        *,
        tape_id: int,
        entry_id: int,
        kind: str,
        embedding: list[float],
    ) -> None:
        embedding = self._normalize_embedding(embedding, self._vector_dimensions)
        cursor = await connection.execute(
            """
            INSERT INTO tape_entry_vector_index (tape_id, entry_id, kind)
            VALUES (?, ?, ?)
            """,
            (tape_id, entry_id, kind),
        )
        try:
            vector_rowid = cursor.lastrowid
        finally:
            await cursor.close()
        if vector_rowid is None:
            raise RuntimeError("Failed to create vector index row.")
        await connection.execute(
            "INSERT INTO tape_entry_vectors (rowid, embedding) VALUES (?, ?)",
            (
                vector_rowid,
                sqlite_vec.serialize_float32(embedding),
            ),
        )

    async def _resolve_bounds(
        self,
        *,
        connection: aiosqlite.Connection,
        tape_id: int,
        query: TapeQuery,
    ) -> tuple[int | None, int | None]:
        if query._between_anchors is not None:
            start_name, end_name = query._between_anchors
            start_id = await self._find_anchor_id(
                connection=connection,
                tape_id=tape_id,
                name=start_name,
                forward=False,
            )
            if start_id is None:
                raise ErrorPayload(
                    ErrorKind.NOT_FOUND, f"Anchor '{start_name}' was not found."
                )
            end_id = await self._find_anchor_id(
                connection=connection,
                tape_id=tape_id,
                name=end_name,
                forward=True,
                after_entry_id=start_id,
            )
            if end_id is None:
                raise ErrorPayload(
                    ErrorKind.NOT_FOUND, f"Anchor '{end_name}' was not found."
                )
            return start_id, end_id

        if query._after_last:
            anchor_id = await self._find_anchor_id(
                connection=connection,
                tape_id=tape_id,
                name=None,
                forward=False,
            )
            if anchor_id is None:
                raise ErrorPayload(ErrorKind.NOT_FOUND, "No anchors found in tape.")
            return anchor_id, None

        if query._after_anchor is not None:
            anchor_id = await self._find_anchor_id(
                connection=connection,
                tape_id=tape_id,
                name=query._after_anchor,
                forward=False,
            )
            if anchor_id is None:
                raise ErrorPayload(
                    ErrorKind.NOT_FOUND,
                    f"Anchor '{query._after_anchor}' was not found.",
                )
            return anchor_id, None

        return None, None

    async def _find_anchor_id(
        self,
        *,
        connection: aiosqlite.Connection,
        tape_id: int,
        name: str | None,
        forward: bool,
        after_entry_id: int = 0,
    ) -> int | None:
        direction = "ASC" if forward else "DESC"
        sql = [
            """
            SELECT id AS entry_id
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
            sql.append("AND id > ?")
            params.append(after_entry_id)

        sql.append(f"ORDER BY id {direction}")
        sql.append("LIMIT 1")
        row = await self._fetchone(connection, "\n".join(sql), params)
        if row is None:
            return None
        return int(row["entry_id"])

    @staticmethod
    def _normalize_kinds(kinds: Iterable[str]) -> list[str]:
        normalized: list[str] = []
        for kind in kinds:
            if not isinstance(kind, str):
                raise TypeError("kinds entries must be strings.")
            normalized.append(kind)
        return normalized

    @staticmethod
    def _normalize_embedding(
        value: Iterable[float] | object,
        expected_dimensions: int | None,
    ) -> list[float]:
        if not isinstance(value, Iterable) or isinstance(
            value, (str, bytes, bytearray)
        ):
            raise TypeError("embedding must be an iterable of numbers.")
        embedding: list[float] = []
        for item in value:
            if not isinstance(item, int | float):
                raise TypeError("embedding values must be numbers.")
            embedding.append(float(item))
        if not embedding:
            raise ValueError("embedding must not be empty.")
        if expected_dimensions is not None and len(embedding) != expected_dimensions:
            raise ValueError(
                f"embedding dimensions must match configured size {expected_dimensions}."
            )
        return embedding

    @classmethod
    def _embedding_response_to_vectors(cls, response: object) -> list[list[float]]:
        data = getattr(response, "data", None)
        if not isinstance(data, list) or not data:
            raise RuntimeError("Embedding response did not include any vectors.")
        vectors: list[list[float]] = []
        for item in data:
            embedding = getattr(item, "embedding", None)
            vectors.append(cls._normalize_embedding(embedding, None))
        return vectors

    @staticmethod
    async def _begin_immediate(connection: aiosqlite.Connection) -> None:
        await connection.execute("BEGIN IMMEDIATE")

    @staticmethod
    async def _fetchone(
        connection: aiosqlite.Connection,
        sql: str,
        params: Iterable[Any] = (),
    ) -> aiosqlite.Row | None:
        cursor = await connection.execute(sql, tuple(params))
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    @staticmethod
    async def _fetchall(
        connection: aiosqlite.Connection,
        sql: str,
        params: Iterable[Any] = (),
    ) -> list[aiosqlite.Row]:
        cursor = await connection.execute(sql, tuple(params))
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    @staticmethod
    def _entry_from_row(row: aiosqlite.Row) -> TapeEntry:
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
    def _text_of(entry: TapeEntry) -> str | None:
        parts = list(SQLiteTapeStore._iter_text_fragments(entry.payload))
        if not parts:
            return None
        return "\n".join(parts)

    @staticmethod
    def _iter_text_fragments(value: object) -> Iterable[str]:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                yield stripped
            return
        if isinstance(value, dict):
            for item in value.values():
                yield from SQLiteTapeStore._iter_text_fragments(item)
            return
        if isinstance(value, list | tuple):
            for item in value:
                yield from SQLiteTapeStore._iter_text_fragments(item)

    @staticmethod
    def _raise_missing_for_query(query: TapeQuery) -> None:
        if query._between_anchors is not None:
            start_name, _ = query._between_anchors
            raise ErrorPayload(
                ErrorKind.NOT_FOUND, f"Anchor '{start_name}' was not found."
            )
        if query._after_last:
            raise ErrorPayload(ErrorKind.NOT_FOUND, "No anchors found in tape.")
        if query._after_anchor is not None:
            raise ErrorPayload(
                ErrorKind.NOT_FOUND, f"Anchor '{query._after_anchor}' was not found."
            )
