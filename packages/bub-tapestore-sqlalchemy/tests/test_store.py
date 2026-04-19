from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from republic import TapeEntry, TapeQuery
from republic.core.results import ErrorPayload

from bub_tapestore_sqlalchemy.store import SQLAlchemyTapeStore


def _store(tmp_path: Path) -> SQLAlchemyTapeStore:
    return SQLAlchemyTapeStore(f"sqlite+pysqlite:///{tmp_path / 'tapes.db'}")


def test_append_list_and_reset_tapes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("a__1", TapeEntry.message({"content": "hello"}))
    store.append("b__2", TapeEntry.system("world"))

    assert store.list_tapes() == ["a__1", "b__2"]

    store.reset("a__1")

    assert store.list_tapes() == ["b__2"]
    assert list(TapeQuery("a__1", store).all()) == []


def test_assigns_monotonic_ids_per_tape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append("room__1", TapeEntry.message({"content": "first"}))
    store.append("room__1", TapeEntry.system("second"))
    store.append("other__1", TapeEntry.system("third"))

    entries = list(TapeQuery("room__1", store).all())
    other_entries = list(TapeQuery("other__1", store).all())

    assert [entry.id for entry in entries] == [1, 2]
    assert [entry.id for entry in other_entries] == [1]


def test_query_after_anchor_and_last_anchor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tape = "session__1"
    store.append(tape, TapeEntry.system("boot"))
    store.append(tape, TapeEntry.anchor("phase-1"))
    store.append(tape, TapeEntry.message({"content": "alpha"}))
    store.append(tape, TapeEntry.anchor("phase-2"))
    store.append(tape, TapeEntry.message({"content": "beta"}))

    after_phase_1 = list(TapeQuery(tape, store).after_anchor("phase-1").all())
    after_last = list(TapeQuery(tape, store).last_anchor().all())

    assert [entry.kind for entry in after_phase_1] == ["message", "anchor", "message"]
    assert [entry.payload.get("content") for entry in after_last] == ["beta"]


def test_query_between_anchors_kinds_and_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tape = "session__2"
    store.append(tape, TapeEntry.anchor("start"))
    store.append(tape, TapeEntry.system("skip"))
    store.append(tape, TapeEntry.message({"content": "one"}))
    store.append(tape, TapeEntry.message({"content": "two"}))
    store.append(tape, TapeEntry.anchor("end"))
    store.append(tape, TapeEntry.message({"content": "three"}))

    entries = list(
        TapeQuery(tape, store)
        .between_anchors("start", "end")
        .kinds("message")
        .limit(1)
        .all()
    )

    assert len(entries) == 1
    assert entries[0].payload == {"content": "one"}


def test_query_kinds_accepts_sequence_input(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tape = "session__kinds"
    store.append(tape, TapeEntry.system("skip"))
    store.append(tape, TapeEntry.message({"content": "one"}))
    store.append(tape, TapeEntry.message({"content": "two"}))

    entries = list(TapeQuery(tape, store).kinds(["message"]).all())

    assert [entry.payload for entry in entries] == [
        {"content": "one"},
        {"content": "two"},
    ]


def test_append_is_safe_across_store_instances(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'shared.db'}"
    tape = "shared__1"
    writers = [SQLAlchemyTapeStore(database_url) for _ in range(4)]

    def _append_range(writer_index: int) -> None:
        store = writers[writer_index]
        for offset in range(25):
            store.append(
                tape,
                TapeEntry.message(
                    {"content": f"writer-{writer_index}", "offset": offset}
                ),
            )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(_append_range, range(4)))

    entries = list(TapeQuery(tape, SQLAlchemyTapeStore(database_url)).all())

    assert len(entries) == 100
    assert [entry.id for entry in entries] == list(range(1, 101))


def test_store_supports_long_tape_and_anchor_names(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tape = "room__" + ("x" * 2048)
    anchor_name = "anchor-" + ("y" * 4096)
    store.append(tape, TapeEntry.anchor(anchor_name))
    store.append(tape, TapeEntry.message({"content": "after long anchor"}))

    entries = list(TapeQuery(tape, store).after_anchor(anchor_name).all())

    assert [entry.payload for entry in entries] == [{"content": "after long anchor"}]


def test_store_rejects_old_schema_database(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE tapes (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            last_entry_id INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE tape_entries (
            tape_id INTEGER NOT NULL,
            entry_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            anchor_name TEXT,
            payload TEXT NOT NULL,
            meta TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (tape_id, entry_id)
        )
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="Delete the old database and recreate it."):
        SQLAlchemyTapeStore(f"sqlite+pysqlite:///{database_path}")


def test_query_missing_anchor_matches_builtin_error_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tape = "session__3"
    store.append(tape, TapeEntry.message({"content": "hello"}))

    with pytest.raises(ErrorPayload, match="Anchor 'missing' was not found."):
        list(TapeQuery(tape, store).after_anchor("missing").all())


def test_store_constructor_validates_url() -> None:
    with pytest.raises(ValueError, match="Invalid SQLAlchemy URL"):
        SQLAlchemyTapeStore("not a sqlalchemy url")


def test_entry_from_payload_round_trip() -> None:
    payload = {
        "id": 7,
        "kind": "message",
        "payload": {"content": "hello"},
        "meta": {"source": "test"},
        "date": "2026-03-08T00:00:00+00:00",
    }

    entry = SQLAlchemyTapeStore.entry_from_payload(payload)

    assert entry is not None
    assert json.loads(json.dumps(entry.payload)) == {"content": "hello"}


def test_query_search_matches_builtin_payload_filtering(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tape = "session__search"
    store.append(tape, TapeEntry.message({"role": "user", "content": "old timeout"}))
    store.append(tape, TapeEntry.message({"role": "user", "content": "new timeout"}))
    store.append(tape, TapeEntry.event("run", {"status": "ok"}))

    entries = list(TapeQuery(tape, store).query("timeout").limit(1).all())

    assert [entry.payload["content"] for entry in entries] == ["new timeout"]


def test_query_search_respects_anchor_bounds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tape = "session__bounded_search"
    store.append(tape, TapeEntry.anchor("phase-1"))
    store.append(tape, TapeEntry.message({"role": "user", "content": "old timeout"}))
    store.append(tape, TapeEntry.anchor("phase-2"))
    store.append(tape, TapeEntry.message({"role": "user", "content": "new timeout"}))

    entries = list(TapeQuery(tape, store).after_anchor("phase-2").query("timeout").all())

    assert [entry.payload["content"] for entry in entries] == ["new timeout"]


def test_query_search_escapes_sql_like_wildcards(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tape = "session__wildcards"
    store.append(tape, TapeEntry.message({"role": "user", "content": "usage is 100%"}))
    store.append(tape, TapeEntry.message({"role": "user", "content": "metric_name"}))

    percent_entries = list(TapeQuery(tape, store).query("100%").all())
    underscore_entries = list(TapeQuery(tape, store).query("metric_name").all())

    assert [entry.payload["content"] for entry in percent_entries] == ["usage is 100%"]
    assert [entry.payload["content"] for entry in underscore_entries] == ["metric_name"]


def test_read_missing_tape_matches_builtin_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.read("missing__tape") == []
