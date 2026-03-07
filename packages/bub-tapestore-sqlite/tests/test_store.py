from __future__ import annotations

from pathlib import Path

import pytest
from republic import TapeEntry, TapeQuery
from republic.core.results import ErrorPayload

from bub_tapestore_sqlite.store import SQLiteTapeStore


def _store(tmp_path: Path) -> SQLiteTapeStore:
    return SQLiteTapeStore(tmp_path / "tapes.sqlite3")


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

    assert [entry.kind for entry in after_phase_1] == [
        "message",
        "anchor",
        "message",
    ]
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


def test_query_missing_anchor_matches_existing_error_shape(tmp_path: Path) -> None:
    store = _store(tmp_path)
    tape = "session__3"
    store.append(tape, TapeEntry.message({"content": "hello"}))

    with pytest.raises(ErrorPayload, match="Anchor 'missing' was not found."):
        list(TapeQuery(tape, store).after_anchor("missing").all())


def test_store_constructor_validates_modes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BUB_TAPESTORE_SQLITE_JOURNAL_MODE"):
        SQLiteTapeStore(tmp_path / "invalid.sqlite3", journal_mode="invalid")
