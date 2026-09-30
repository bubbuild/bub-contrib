from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from bub import hookimpl
from bub.framework import BubFramework
from bub.tape import AsyncTapeStoreAdapter, InMemoryTapeStore, TapeEntry
from tape_dataset_opendal import plugin
from typer.testing import CliRunner


class _StorePlugin:
    def __init__(self, store: InMemoryTapeStore) -> None:
        self._store = store

    @hookimpl
    def provide_tape_store(self) -> InMemoryTapeStore:
        return self._store


@pytest.mark.parametrize("async_store", [False, True])
def test_plugin_registers_bub_cli_export_command(
    tmp_path: Path, async_store: bool
) -> None:
    framework = BubFramework()

    store = InMemoryTapeStore()
    store.append("ops__1", TapeEntry.anchor("triage"))
    store.append(
        "ops__1", TapeEntry.message({"role": "user", "content": "Database timeout"})
    )

    events: list[str] = []

    class StorePlugin:
        @hookimpl
        async def provide_tape_store(self) -> AsyncIterator:
            events.append("enter")
            try:
                yield AsyncTapeStoreAdapter(store) if async_store else store
            finally:
                events.append("exit")

    framework.plugin_manager.register(StorePlugin(), name="test-store")
    framework.plugin_manager.register(plugin, name="tape-dataset-opendal")

    app = framework.create_cli_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tape-export",
            "--scheme",
            "fs",
            "--config",
            f"root={tmp_path}",
            "--root",
            "dataset",
        ],
        obj=framework,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["tape_count"] == 1
    assert payload["entry_count"] == 2
    assert (tmp_path / "dataset" / "manifest.json").is_file()
    assert events == ["enter", "exit"]
    assert framework.get_tape_store() is None


def test_plugin_cli_accepts_cel_filter_file(tmp_path: Path) -> None:
    framework = BubFramework()

    store = InMemoryTapeStore()
    store.append("ops__1", TapeEntry.anchor("triage"))
    store.append(
        "ops__1", TapeEntry.message({"role": "user", "content": "Database timeout"})
    )
    store.append(
        "ops__1", TapeEntry.message({"role": "assistant", "content": "Ignore me"})
    )

    filter_file = tmp_path / "filters.cel"
    filter_file.write_text(
        '# keep only user messages\nkind == "message"\npayload.role == "user"\n',
        encoding="utf-8",
    )

    framework.plugin_manager.register(_StorePlugin(store), name="test-store")
    framework.plugin_manager.register(plugin, name="tape-dataset-opendal")

    app = framework.create_cli_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tape-export",
            "--scheme",
            "fs",
            "--config",
            f"root={tmp_path}",
            "--root",
            "dataset-filtered",
            "--filter-file",
            str(filter_file),
        ],
        obj=framework,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    manifest = json.loads(
        (tmp_path / "dataset-filtered" / "manifest.json").read_text(encoding="utf-8")
    )
    entries = [
        json.loads(line)
        for line in (tmp_path / "dataset-filtered" / "entries.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert payload["entry_count"] == 1
    assert manifest["filters"] == ['kind == "message"', 'payload.role == "user"']
    assert entries[0]["entry"]["payload"]["content"] == "Database timeout"
