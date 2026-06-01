from __future__ import annotations

from republic import TapeEntry

from bub_tapestore_otel.exporter import _batch_attributes, _entry_attributes, _should_flush_batch, _span_name


def test_span_name_maps_known_tape_events() -> None:
    assert _span_name(TapeEntry.event("run", data={})) == "bub.model.run"
    assert _span_name(TapeEntry.event("loop.step", data={})) == "bub.loop.step"
    assert _span_name(TapeEntry.event("command", data={})) == "bub.command"


def test_entry_attributes_do_not_include_content() -> None:
    entry = TapeEntry.event(
        "run",
        data={
            "status": "ok",
            "elapsed_ms": 12,
            "usage": {"total_tokens": 42},
            "prompt": "do not export",
        },
    )

    attributes = _entry_attributes("tape-1", entry)

    assert attributes["bub.tape.name"] == "tape-1"
    assert attributes["bub.tape.entry.kind"] == "event"
    assert attributes["bub.tape.entry.name"] == "run"
    assert attributes["bub.duration_ms"] == 12
    assert attributes["bub.usage.total_tokens"] == 42
    assert "prompt" not in attributes


def test_entry_attributes_include_safe_shape_metadata() -> None:
    entry = TapeEntry.event(
        "loop.start",
        data={
            "model": "openai:gpt-5",
            "prompt": "hello",
        },
    )

    attributes = _entry_attributes("tape-1", entry)

    assert attributes["bub.model"] == "openai:gpt-5"
    assert attributes["bub.prompt.chars"] == 5
    assert "hello" not in attributes.values()


def test_batch_flushes_on_completed_tape_turn_markers() -> None:
    assert _should_flush_batch(TapeEntry.event("loop.step", data={"status": "ok"}))
    assert _should_flush_batch(TapeEntry.event("command", data={}))
    assert not _should_flush_batch(TapeEntry.event("loop.step.start", data={}))


def test_batch_attributes_summarize_entry_range() -> None:
    entries = [
        TapeEntry.event("loop.start", data={}),
        TapeEntry.event("loop.step", data={"status": "ok"}),
    ]

    attributes = _batch_attributes("tape-1", entries)

    assert attributes["bub.tape.name"] == "tape-1"
    assert attributes["bub.tape.batch.entries"] == 2
    assert attributes["bub.tape.batch.first_entry_id"] == entries[0].id
    assert attributes["bub.tape.batch.last_entry_id"] == entries[-1].id
