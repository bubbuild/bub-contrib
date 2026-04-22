from __future__ import annotations

import base64
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from opendal import AsyncOperator, Operator
from republic.tape.entries import TapeEntry
from republic.tape.query import TapeQuery
from republic.tape.store import AsyncTapeStore, AsyncTapeStoreAdapter, TapeStore, is_async_tape_store

from tape_dataset_opendal.filters import EntryFilter
from tape_dataset_opendal.models import ExportLayout, ExportReport, utc_now


def export_dataset(
    store: TapeStore,
    operator: Operator,
    *,
    layout: ExportLayout | None = None,
    entry_filter: EntryFilter | None = None,
) -> ExportReport:
    export_layout = layout or ExportLayout()
    compiled_filter = entry_filter or EntryFilter()
    entries_by_tape = {
        tape: _filter_entries(tape, list(TapeQuery(tape=tape, store=store).all()), compiled_filter)
        for tape in store.list_tapes()
    }
    files, report = _build_export(entries_by_tape, layout=export_layout, entry_filter=compiled_filter)
    _prepare_directories(operator, export_layout)
    for path, payload in files.items():
        operator.write(path, payload)
    return report


async def export_dataset_async(
    store: TapeStore | AsyncTapeStore,
    operator: Operator | AsyncOperator,
    *,
    layout: ExportLayout | None = None,
    entry_filter: EntryFilter | None = None,
) -> ExportReport:
    export_layout = layout or ExportLayout()
    compiled_filter = entry_filter or EntryFilter()
    async_store = store if is_async_tape_store(store) else AsyncTapeStoreAdapter(store)
    tape_names = await async_store.list_tapes()
    entries_by_tape: dict[str, list[TapeEntry]] = {}
    for tape in tape_names:
        entries = list(await TapeQuery(tape=tape, store=async_store).all())
        entries_by_tape[tape] = _filter_entries(tape, entries, compiled_filter)

    files, report = _build_export(entries_by_tape, layout=export_layout, entry_filter=compiled_filter)
    async_operator = operator if isinstance(operator, AsyncOperator) else operator.to_async_operator()
    await _prepare_directories_async(async_operator, export_layout)
    for path, payload in files.items():
        await async_operator.write(path, payload)
    return report


def _build_export(
    entries_by_tape: Mapping[str, Sequence[TapeEntry]],
    *,
    layout: ExportLayout,
    entry_filter: EntryFilter,
) -> tuple[dict[str, bytes], ExportReport]:
    exported_at = utc_now()
    sorted_tapes = sorted(tape for tape, entries in entries_by_tape.items() if entries)
    files: dict[str, bytes] = {}
    tape_rows: list[dict[str, Any]] = []
    entry_rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []
    total_entries = 0

    for tape in sorted_tapes:
        entries = [entry.copy() for entry in entries_by_tape[tape]]
        total_entries += len(entries)
        raw_path = ""
        if layout.include_raw_tapes:
            raw_path = _path(layout, layout.raw_dir, f"{_encode_tape_name(tape)}.jsonl")
            files[raw_path] = _jsonl_bytes(_raw_entry_record(entry) for entry in entries)
        segments = _segment_rows(tape, entries) if layout.include_segments else []
        segment_rows.extend(segments)
        tape_rows.append(
            {
                "tape": tape,
                "entry_count": len(entries),
                "anchor_count": sum(1 for entry in entries if entry.kind == "anchor"),
                "segment_count": len(segments),
                "first_date": entries[0].date if entries else None,
                "last_date": entries[-1].date if entries else None,
                "kinds": dict(sorted(Counter(entry.kind for entry in entries).items())),
                "raw_path": raw_path or None,
            }
        )
        entry_rows.extend(_entry_row(tape, entry) for entry in entries)

    manifest_path = _path(layout, layout.manifest_name)
    tapes_path = _path(layout, layout.tapes_name)
    entries_path = _path(layout, layout.entries_name)
    files[tapes_path] = _jsonl_bytes(tape_rows)
    files[entries_path] = _jsonl_bytes(entry_rows)

    file_paths = [manifest_path, tapes_path, entries_path, *files.keys()]
    if layout.include_segments:
        segments_path = _path(layout, layout.segments_name)
        files[segments_path] = _jsonl_bytes(segment_rows)
        file_paths.append(segments_path)

    manifest = {
        "format": "tape.dataset",
        "version": 1,
        "exported_at": exported_at,
        "root": layout.root or ".",
        "tape_count": len(sorted_tapes),
        "entry_count": total_entries,
        "segment_count": len(segment_rows),
        "files": {
            "manifest": manifest_path,
            "tapes": tapes_path,
            "entries": entries_path,
            "segments": _path(layout, layout.segments_name) if layout.include_segments else None,
            "raw_dir": _path(layout, layout.raw_dir) if layout.include_raw_tapes else None,
        },
        "filters": list(entry_filter.expressions),
    }
    files[manifest_path] = _json_bytes(manifest)
    deduped_files = tuple(dict.fromkeys(file_paths + list(files)).keys())
    return (
        files,
        ExportReport(
            exported_at=exported_at,
            root=layout.root,
            tape_count=len(sorted_tapes),
            entry_count=total_entries,
            segment_count=len(segment_rows),
            manifest_path=manifest_path,
            files=deduped_files,
        ),
    )


def _segment_rows(tape: str, entries: Sequence[TapeEntry]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    anchored = [entry for entry in entries if entry.kind == "anchor"]
    if not anchored:
        if not entries:
            return []
        rows.append(
            {
                "segment_id": f"{tape}:full:1",
                "segment_kind": "full_tape",
                "tape": tape,
                "anchor": None,
                "end_anchor": None,
                "entry_ids": [entry.id for entry in entries],
                "entries": [_raw_entry_record(entry) for entry in entries],
            }
        )
        return rows

    segment_index = 0
    active_anchor: TapeEntry | None = None
    active_entries: list[TapeEntry] = []

    for entry in entries:
        if entry.kind == "anchor":
            if active_anchor is not None and active_entries:
                segment_index += 1
                rows.append(
                    _segment_row(
                        tape=tape,
                        segment_index=segment_index,
                        anchor=active_anchor,
                        end_anchor=entry,
                        entries=active_entries,
                    )
                )
            active_anchor = entry
            active_entries = []
            continue
        if active_anchor is not None:
            active_entries.append(entry)

    if active_anchor is not None and active_entries:
        segment_index += 1
        rows.append(
            _segment_row(
                tape=tape,
                segment_index=segment_index,
                anchor=active_anchor,
                end_anchor=None,
                entries=active_entries,
            )
        )
    return rows


def _segment_row(
    *,
    tape: str,
    segment_index: int,
    anchor: TapeEntry,
    end_anchor: TapeEntry | None,
    entries: Sequence[TapeEntry],
) -> dict[str, Any]:
    return {
        "segment_id": f"{tape}:anchor:{segment_index}",
        "segment_kind": "anchor_slice",
        "tape": tape,
        "anchor": _anchor_record(anchor),
        "end_anchor": _anchor_record(end_anchor) if end_anchor is not None else None,
        "entry_ids": [entry.id for entry in entries],
        "entries": [_raw_entry_record(entry) for entry in entries],
    }


def _anchor_record(entry: TapeEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "name": entry.payload.get("name"),
        "date": entry.date,
        "payload": dict(entry.payload),
        "meta": dict(entry.meta),
    }


def _entry_row(tape: str, entry: TapeEntry) -> dict[str, Any]:
    return {
        "tape": tape,
        "entry": _raw_entry_record(entry),
    }


def _raw_entry_record(entry: TapeEntry) -> dict[str, Any]:
    payload = asdict(entry)
    payload["payload"] = dict(entry.payload)
    payload["meta"] = dict(entry.meta)
    return payload


def _encode_tape_name(tape: str) -> str:
    return base64.urlsafe_b64encode(tape.encode("utf-8")).decode("ascii").rstrip("=")


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Sequence[dict[str, Any]] | Any) -> bytes:
    return b"".join(_json_bytes(row) for row in rows)


def _path(layout: ExportLayout, *parts: str) -> str:
    cleaned = [part.strip("/") for part in parts if part and part.strip("/")]
    if layout.root:
        cleaned.insert(0, layout.root)
    return "/".join(cleaned)


def _prepare_directories(operator: Operator, layout: ExportLayout) -> None:
    for directory in _directories(layout):
        operator.create_dir(directory)


async def _prepare_directories_async(operator: AsyncOperator, layout: ExportLayout) -> None:
    for directory in _directories(layout):
        await operator.create_dir(directory)


def _directories(layout: ExportLayout) -> list[str]:
    directories: list[str] = []
    if layout.root:
        directories.append(f"{layout.root}/")
    if layout.include_raw_tapes:
        directories.append(f"{_path(layout, layout.raw_dir)}/")
    return directories


def _filter_entries(tape: str, entries: Sequence[TapeEntry], entry_filter: EntryFilter) -> list[TapeEntry]:
    return [entry.copy() for entry in entries if entry_filter.matches(tape, entry)]
