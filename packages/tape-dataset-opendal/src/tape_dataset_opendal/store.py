from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from opendal import AsyncOperator, Operator
from republic.tape.entries import TapeEntry
from republic.tape.query import TapeQuery
from republic.tape.store import AsyncTapeStore, AsyncTapeStoreAdapter, TapeStore, is_async_tape_store

from tape_dataset_opendal.exporter import export_dataset, export_dataset_async
from tape_dataset_opendal.filters import EntryFilter
from tape_dataset_opendal.models import ExportLayout, ExportReport


class ExportableTapeStore:
    """Sync TapeStore wrapper with OpenDAL dataset export helpers."""

    def __init__(self, store: TapeStore) -> None:
        self._store = store

    def list_tapes(self) -> list[str]:
        return self._store.list_tapes()

    def reset(self, tape: str) -> None:
        self._store.reset(tape)

    def fetch_all(self, query: TapeQuery[Any]) -> Iterable[TapeEntry]:
        return self._store.fetch_all(query)

    def append(self, tape: str, entry: TapeEntry) -> None:
        self._store.append(tape, entry)

    def export_dataset(
        self,
        operator: Operator,
        *,
        layout: ExportLayout | None = None,
        entry_filter: EntryFilter | None = None,
    ) -> ExportReport:
        return export_dataset(self._store, operator, layout=layout, entry_filter=entry_filter)


class AsyncExportableTapeStore:
    """Async TapeStore wrapper with OpenDAL dataset export helpers."""

    def __init__(self, store: TapeStore | AsyncTapeStore) -> None:
        self._store = store if is_async_tape_store(store) else AsyncTapeStoreAdapter(store)

    async def list_tapes(self) -> list[str]:
        return await self._store.list_tapes()

    async def reset(self, tape: str) -> None:
        await self._store.reset(tape)

    async def fetch_all(self, query: TapeQuery[Any]) -> Iterable[TapeEntry]:
        return await self._store.fetch_all(query)

    async def append(self, tape: str, entry: TapeEntry) -> None:
        await self._store.append(tape, entry)

    async def export_dataset_async(
        self,
        operator: Operator | AsyncOperator,
        *,
        layout: ExportLayout | None = None,
        entry_filter: EntryFilter | None = None,
    ) -> ExportReport:
        return await export_dataset_async(self._store, operator, layout=layout, entry_filter=entry_filter)
