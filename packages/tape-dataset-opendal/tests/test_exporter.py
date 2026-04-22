from __future__ import annotations

import asyncio
import json

import opendal
from republic.tape.entries import TapeEntry
from republic.tape.query import TapeQuery
from republic.tape.store import AsyncTapeStoreAdapter, InMemoryTapeStore

from tape_dataset_opendal import (
    AsyncExportableTapeStore,
    EntryFilter,
    ExportLayout,
    ExportableTapeStore,
)


def _read_json(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_sync_wrapper_delegates_standard_tape_store_contract() -> None:
    inner = InMemoryTapeStore()
    store = ExportableTapeStore(inner)

    store.append("ops__1", TapeEntry.anchor("triage"))
    store.append("ops__1", TapeEntry.message({"role": "user", "content": "Database timeout"}))

    assert store.list_tapes() == ["ops__1"]
    entries = list(store.fetch_all(TapeQuery("ops__1", store)))
    assert [entry.kind for entry in entries] == ["anchor", "message"]

    store.reset("ops__1")

    assert store.list_tapes() == []


def test_sync_export_writes_manifest_entries_segments_and_raw_files(tmp_path) -> None:
    inner = InMemoryTapeStore()
    inner.append("ops__1", TapeEntry.anchor("triage", {"owner": "db"}))
    inner.append("ops__1", TapeEntry.message({"role": "user", "content": "Database timeout"}))
    inner.append("ops__1", TapeEntry.message({"role": "assistant", "content": "Check pool saturation"}))
    inner.append("chat__2", TapeEntry.system("boot"))
    inner.append("chat__2", TapeEntry.message({"role": "assistant", "content": "hello"}))

    store = ExportableTapeStore(inner)
    operator = opendal.Operator("fs", root=str(tmp_path))

    report = store.export_dataset(operator, layout=ExportLayout(root="dataset"))

    manifest = _read_json(tmp_path / "dataset" / "manifest.json")
    tape_rows = _read_jsonl(tmp_path / "dataset" / "tapes.jsonl")
    entry_rows = _read_jsonl(tmp_path / "dataset" / "entries.jsonl")
    segment_rows = _read_jsonl(tmp_path / "dataset" / "segments.jsonl")
    raw_files = sorted((tmp_path / "dataset" / "raw").glob("*.jsonl"))

    assert report.tape_count == 2
    assert report.entry_count == 5
    assert report.segment_count == 2
    assert manifest["format"] == "tape.dataset"
    assert manifest["tape_count"] == 2
    assert [row["tape"] for row in tape_rows] == ["chat__2", "ops__1"]
    assert len(entry_rows) == 5
    assert segment_rows[0]["segment_kind"] == "full_tape"
    assert segment_rows[1]["anchor"]["name"] == "triage"
    assert len(raw_files) == 2


def test_async_export_supports_async_tape_store_and_async_operator(tmp_path) -> None:
    async def scenario() -> None:
        inner = AsyncTapeStoreAdapter(InMemoryTapeStore())
        store = AsyncExportableTapeStore(inner)

        await store.append("agent__1", TapeEntry.anchor("task"))
        await store.append("agent__1", TapeEntry.tool_call([{"id": "call_1", "name": "search"}]))
        await store.append("agent__1", TapeEntry.tool_result([{"ok": True}]))

        operator = opendal.AsyncOperator("fs", root=str(tmp_path))
        report = await store.export_dataset_async(operator, layout=ExportLayout(root="dataset"))

        entries = list(await TapeQuery("agent__1", inner).all())
        manifest = _read_json(tmp_path / "dataset" / "manifest.json")
        segment_rows = _read_jsonl(tmp_path / "dataset" / "segments.jsonl")

        assert len(entries) == 3
        assert report.segment_count == 1
        assert manifest["entry_count"] == 3
        assert segment_rows == [
            {
                "anchor": {
                    "date": entries[0].date,
                    "id": 1,
                    "meta": {},
                    "name": "task",
                    "payload": {"name": "task"},
                },
                "end_anchor": None,
                "entries": [
                    {
                        "date": entries[1].date,
                        "id": 2,
                        "kind": "tool_call",
                        "meta": {},
                        "payload": {"calls": [{"id": "call_1", "name": "search"}]},
                    },
                    {
                        "date": entries[2].date,
                        "id": 3,
                        "kind": "tool_result",
                        "meta": {},
                        "payload": {"results": [{"ok": True}]},
                    },
                ],
                "entry_ids": [2, 3],
                "segment_id": "agent__1:anchor:1",
                "segment_kind": "anchor_slice",
                "tape": "agent__1",
            }
        ]

    asyncio.run(scenario())


def test_export_supports_cel_entry_filter_and_records_it_in_manifest(tmp_path) -> None:
    inner = InMemoryTapeStore()
    inner.append("ops__1", TapeEntry.anchor("triage", {"owner": "db"}))
    inner.append("ops__1", TapeEntry.message({"role": "user", "content": "Database timeout"}))
    inner.append("ops__1", TapeEntry.message({"role": "assistant", "content": "Check pool saturation"}))
    inner.append("chat__2", TapeEntry.message({"role": "assistant", "content": "hello"}))

    store = ExportableTapeStore(inner)
    operator = opendal.Operator("fs", root=str(tmp_path))
    report = store.export_dataset(
        operator,
        layout=ExportLayout(root="dataset"),
        entry_filter=EntryFilter(
            [
                'kind == "message"',
                'payload.role == "user" || text.contains("hello")',
            ]
        ),
    )

    manifest = _read_json(tmp_path / "dataset" / "manifest.json")
    tape_rows = _read_jsonl(tmp_path / "dataset" / "tapes.jsonl")
    entry_rows = _read_jsonl(tmp_path / "dataset" / "entries.jsonl")
    raw_files = sorted((tmp_path / "dataset" / "raw").glob("*.jsonl"))

    assert report.tape_count == 2
    assert report.entry_count == 2
    assert report.segment_count == 2
    assert manifest["filters"] == ['kind == "message"', 'payload.role == "user" || text.contains("hello")']
    assert [row["tape"] for row in tape_rows] == ["chat__2", "ops__1"]
    assert [row["entry"]["payload"]["content"] for row in entry_rows] == ["hello", "Database timeout"]
    assert len(raw_files) == 2
