from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ExportLayout:
    root: str = ""
    include_segments: bool = True
    include_raw_tapes: bool = True
    manifest_name: str = "manifest.json"
    tapes_name: str = "tapes.jsonl"
    entries_name: str = "entries.jsonl"
    segments_name: str = "segments.jsonl"
    raw_dir: str = "raw"

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.strip("/"))


@dataclass(frozen=True)
class ExportReport:
    exported_at: str
    root: str
    tape_count: int
    entry_count: int
    segment_count: int
    manifest_path: str
    files: tuple[str, ...] = field(default_factory=tuple)
