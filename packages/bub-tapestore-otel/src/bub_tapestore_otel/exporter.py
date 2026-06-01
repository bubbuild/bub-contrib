from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any

from loguru import logger
from republic import TapeEntry

SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class LogfireTapeExporterSettings:
    service_name: str = "bub"
    send_to_logfire: bool = False
    force_flush: bool = True
    shutdown_after_flush: bool = True


class LogfireTapeExporter:
    def __init__(self, settings: LogfireTapeExporterSettings | None = None) -> None:
        self._settings = settings or LogfireTapeExporterSettings()
        self._configured = False
        self._lock = threading.Lock()
        self._pending: dict[str, list[TapeEntry]] = {}

    def append(self, tape: str, entry: TapeEntry) -> None:
        try:
            self._append(tape, entry)
        except Exception:
            logger.opt(exception=True).warning("tapestore.otel.export_failed action=append tape={}", tape)

    def reset(self, tape: str) -> None:
        try:
            self._reset(tape)
        except Exception:
            logger.opt(exception=True).warning("tapestore.otel.export_failed action=reset tape={}", tape)

    def _configure(self) -> None:
        if self._configured:
            return
        import logfire

        logfire.configure(
            send_to_logfire=self._settings.send_to_logfire,
            service_name=self._settings.service_name,
            console=False,
            scrubbing=False,
        )
        self._configured = True

    def _flush(self) -> None:
        if not self._settings.force_flush:
            return
        import logfire

        logfire.force_flush()
        if self._settings.shutdown_after_flush:
            logfire.shutdown()
            self._configured = False

    def _append(self, tape: str, entry: TapeEntry) -> None:
        self._configure()
        batch = self._record_entry(tape, entry)
        if batch is None:
            return
        _instrument_batch(tape, batch)
        self._flush()

    def _reset(self, tape: str) -> None:
        self._configure()
        batch = self._pop_pending(tape)
        if batch:
            _instrument_batch(tape, batch)
        _instrument_reset(tape)
        self._flush()

    def _record_entry(self, tape: str, entry: TapeEntry) -> list[TapeEntry] | None:
        with self._lock:
            entries = self._pending.setdefault(tape, [])
            entries.append(entry)
            if not _should_flush_batch(entry):
                return None
            return self._pending.pop(tape)

    def _pop_pending(self, tape: str) -> list[TapeEntry]:
        with self._lock:
            return self._pending.pop(tape, [])


def _entry_name(entry: TapeEntry) -> str:
    value = entry.payload.get("name")
    return str(value) if value else entry.kind


def _span_name(entry: TapeEntry) -> str:
    name = _entry_name(entry)
    if entry.kind == "event" and name == "run":
        return "bub.model.run"
    if entry.kind == "event" and name.startswith("loop.step"):
        return "bub.loop.step"
    if entry.kind == "event" and name == "command":
        return "bub.command"
    if entry.kind == "anchor" and name != "session/start":
        return "bub.tape.handoff"
    safe_name = SAFE_NAME_RE.sub(".", name).strip(".") or entry.kind
    return f"bub.tape.{entry.kind}.{safe_name}"


def _payload_data(entry: TapeEntry) -> dict[str, Any]:
    data = entry.payload.get("data")
    return data if isinstance(data, dict) else {}


def _entry_attributes(tape: str, entry: TapeEntry) -> dict[str, Any]:
    data = _payload_data(entry)
    attributes: dict[str, Any] = {
        "bub.tape.name": tape,
        "bub.tape.entry.id": entry.id,
        "bub.tape.entry.kind": entry.kind,
        "bub.tape.entry.name": _entry_name(entry),
        "bub.tape.entry.date": entry.date,
    }
    for source_key, attr_key in (
        ("status", "bub.tape.entry.status"),
        ("step", "bub.loop.step"),
        ("elapsed_ms", "bub.duration_ms"),
        ("model", "bub.model"),
        ("provider", "bub.provider"),
    ):
        if source_key in data:
            attributes[attr_key] = data[source_key]

    prompt = data.get("prompt")
    if isinstance(prompt, str):
        attributes["bub.prompt.chars"] = len(prompt)
    elif isinstance(prompt, list):
        attributes["bub.prompt.parts"] = len(prompt)

    content = entry.payload.get("content")
    if isinstance(content, str):
        attributes["bub.content.chars"] = len(content)

    usage = data.get("usage")
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if key in usage:
                attributes[f"bub.usage.{key}"] = usage[key]

    return attributes


def _batch_attributes(tape: str, entries: list[TapeEntry]) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "bub.tape.name": tape,
        "bub.tape.batch.entries": len(entries),
    }
    if entries:
        attributes["bub.tape.batch.first_entry_id"] = entries[0].id
        attributes["bub.tape.batch.last_entry_id"] = entries[-1].id
        attributes["bub.tape.batch.first_entry_date"] = entries[0].date
        attributes["bub.tape.batch.last_entry_date"] = entries[-1].date
    return attributes


def _should_flush_batch(entry: TapeEntry) -> bool:
    if entry.kind == "event" and _entry_name(entry) in {"command", "loop.step"}:
        return True
    return False


def _instrument_batch(tape: str, entries: list[TapeEntry]) -> None:
    import logfire

    @logfire.instrument(
        "bub.tape.export",
        span_name="bub.tape.export",
        extract_args=False,
    )
    def emit() -> None:
        with logfire.span(
            "bub.tape.batch {tape}",
            _span_name="bub.tape.batch",
            tape=tape,
            **_batch_attributes(tape, entries),
        ):
            for entry in entries:
                with logfire.span(
                    "bub.tape.entry {entry_name}",
                    _span_name=_span_name(entry),
                    entry_name=_entry_name(entry),
                    **_entry_attributes(tape, entry),
                ):
                    pass

    emit()


def _instrument_reset(tape: str) -> None:
    import logfire

    @logfire.instrument(
        "bub.tape.reset",
        span_name="bub.tape.reset",
        extract_args=False,
    )
    def emit() -> None:
        with logfire.span(
            "bub.tape.reset {tape}",
            _span_name="bub.tape.reset",
            **{"bub.tape.name": tape},
        ):
            pass

    emit()
