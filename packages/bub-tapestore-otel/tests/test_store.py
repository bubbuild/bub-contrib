from __future__ import annotations

from collections.abc import Iterable

import pytest
from bub_tapestore_otel.store import OTelTapeStore
from republic import TapeEntry, TapeQuery


class MemoryStore:
    def __init__(self) -> None:
        self.entries: dict[str, list[TapeEntry]] = {}
        self.resets: list[str] = []

    def list_tapes(self) -> list[str]:
        return sorted(self.entries)

    def reset(self, tape: str) -> None:
        self.resets.append(tape)
        self.entries[tape] = []

    def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]:
        return list(self.entries.get(query.tape, []))

    def append(self, tape: str, entry: TapeEntry) -> None:
        self.entries.setdefault(tape, []).append(entry)


class Exporter:
    def __init__(self) -> None:
        self.appended: list[tuple[str, TapeEntry]] = []
        self.reset_tapes: list[str] = []

    def append(self, tape: str, entry: TapeEntry) -> None:
        self.appended.append((tape, entry))

    def reset(self, tape: str) -> None:
        self.reset_tapes.append(tape)


class FailingExporter(Exporter):
    def append(self, tape: str, entry: TapeEntry) -> None:
        raise RuntimeError("export failed")


@pytest.mark.asyncio
async def test_append_writes_inner_store_before_exporting() -> None:
    inner = MemoryStore()
    exporter = Exporter()
    store = OTelTapeStore(inner, exporter)
    entry = TapeEntry.event("loop.step", data={"status": "ok"})

    await store.append("tape-1", entry)

    assert inner.entries == {"tape-1": [entry]}
    assert exporter.appended == [("tape-1", entry)]


@pytest.mark.asyncio
async def test_reset_writes_inner_store_before_exporting() -> None:
    inner = MemoryStore()
    exporter = Exporter()
    store = OTelTapeStore(inner, exporter)

    await store.reset("tape-1")

    assert inner.resets == ["tape-1"]
    assert exporter.reset_tapes == ["tape-1"]


@pytest.mark.asyncio
async def test_export_errors_do_not_roll_back_inner_write() -> None:
    inner = MemoryStore()
    store = OTelTapeStore(inner, FailingExporter())
    entry = TapeEntry.event("command", data={})

    await store.append("tape-1", entry)

    assert inner.entries == {"tape-1": [entry]}
