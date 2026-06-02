from __future__ import annotations

import json
from pathlib import Path

import pytest

from bub_pages.config import PagesSettings, PagesStore, SiteConfig


def _settings(tmp_path: Path) -> PagesSettings:
    return PagesSettings(
        config_path=tmp_path / "pages.json", pages_root=tmp_path / "pages"
    )


def test_store_adds_and_removes_site(tmp_path: Path) -> None:
    store = PagesStore(_settings(tmp_path))
    site = SiteConfig(name="docs", artifact=tmp_path / "site", path="/docs")

    store.add(site)

    config = store.read()
    assert config.sites["docs"] == site
    assert json.loads((tmp_path / "pages.json").read_text(encoding="utf-8")) == {
        "sites": {
            "docs": {
                "artifact": str(tmp_path / "site"),
                "path": "/docs",
            }
        }
    }

    removed = store.remove("docs")

    assert removed == site
    assert store.read().sites == {}


def test_store_rejects_duplicate_names_without_replace(tmp_path: Path) -> None:
    store = PagesStore(_settings(tmp_path))
    artifact = tmp_path / "site"
    store.add(SiteConfig(name="docs", artifact=artifact, path="/docs"))

    with pytest.raises(ValueError, match="already exists"):
        store.add(SiteConfig(name="docs", artifact=artifact, path="/new-docs"))


def test_store_rejects_duplicate_paths(tmp_path: Path) -> None:
    store = PagesStore(_settings(tmp_path))
    artifact = tmp_path / "site"
    store.add(SiteConfig(name="docs", artifact=artifact, path="/docs"))

    with pytest.raises(ValueError, match="both use path"):
        store.add(SiteConfig(name="manual", artifact=artifact, path="/docs"))


def test_store_reads_empty_config_when_missing(tmp_path: Path) -> None:
    assert PagesStore(_settings(tmp_path)).read().sites == {}
