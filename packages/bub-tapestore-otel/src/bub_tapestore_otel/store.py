from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from loguru import logger
from republic import AsyncTapeStore, TapeEntry, TapeQuery
from republic.tape import TapeStore
from republic.tape.store import is_async_tape_store


class TapeExporter(Protocol):
    def append(self, tape: str, entry: TapeEntry) -> None: ...

    def reset(self, tape: str) -> None: ...


class OTelTapeStore:
    """Transparent async tape-store decorator that observes committed writes."""

    def __init__(self, inner: TapeStore | AsyncTapeStore, exporter: TapeExporter) -> None:
        self._inner = inner
        self._exporter = exporter

    async def list_tapes(self) -> list[str]:
        if is_async_tape_store(self._inner):
            return await self._inner.list_tapes()
        return self._inner.list_tapes()

    async def fetch_all(self, query: TapeQuery[AsyncTapeStore]) -> Iterable[TapeEntry]:
        if is_async_tape_store(self._inner):
            return await self._inner.fetch_all(query)
        return self._inner.fetch_all(query)

    async def append(self, tape: str, entry: TapeEntry) -> None:
        if is_async_tape_store(self._inner):
            await self._inner.append(tape, entry)
        else:
            self._inner.append(tape, entry)
        try:
            self._exporter.append(tape, entry)
        except Exception:
            logger.opt(exception=True).warning("tapestore.otel.export_failed action=append tape={}", tape)

    async def reset(self, tape: str) -> None:
        if is_async_tape_store(self._inner):
            await self._inner.reset(tape)
        else:
            self._inner.reset(tape)
        try:
            self._exporter.reset(tape)
        except Exception:
            logger.opt(exception=True).warning("tapestore.otel.export_failed action=reset tape={}", tape)
