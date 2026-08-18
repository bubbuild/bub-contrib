from __future__ import annotations

from pathlib import Path

import pytest

from bub_qq.store import QQPlatformStore


def test_store_roundtrip_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = QQPlatformStore(path)

    store.update("group", "group-1", active_messages=True, require_mention="mention")
    store.update("c2c", "user-1", active_messages=False)

    reloaded = QQPlatformStore(path)
    assert reloaded.active_messages_allowed("group", "group-1") is True
    assert reloaded.require_mention("group-1") == "mention"
    assert reloaded.active_messages_allowed("c2c", "user-1") is False


def test_store_defaults_to_unknown_state(tmp_path: Path) -> None:
    store = QQPlatformStore(tmp_path / "state.json")

    assert store.active_messages_allowed("group", "never-seen") is None
    assert store.require_mention("never-seen") is None
    assert store.get("group", "never-seen") == {}


def test_store_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    store = QQPlatformStore(path)
    assert store.get("group", "group-1") == {}

    store.update("group", "group-1", active_messages=True)
    assert QQPlatformStore(path).active_messages_allowed("group", "group-1") is True


def test_store_update_merges_fields(tmp_path: Path) -> None:
    store = QQPlatformStore(tmp_path / "state.json")

    store.update("group", "group-1", active_messages=True)
    store.update("group", "group-1", require_mention="always")

    assert store.get("group", "group-1") == {
        "active_messages": True,
        "require_mention": "always",
    }


def test_store_rejects_unknown_scope(tmp_path: Path) -> None:
    store = QQPlatformStore(tmp_path / "state.json")

    with pytest.raises(ValueError, match="unknown qq store scope"):
        store.get("guild", "id")
