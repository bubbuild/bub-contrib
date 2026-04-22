# tape-dataset-opendal

`tape-dataset-opendal` exports standard Republic/Bub tapes to a backend-agnostic dataset layout through Apache OpenDAL.

The package is intentionally narrow:

- source side: any standard `TapeStore` or `AsyncTapeStore`
- sink side: any OpenDAL backend
- export shape: tape-native records plus tape-level and anchor-derived views
- Bub integration: a thin CLI layer over the same exporter

It does not redefine tape semantics or impose an application-specific session schema.

## Install

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/tape-dataset-opendal"
```

You can also install it with Bub:

```bash
bub install tape-dataset-opendal@main
```

## What This Package Does

Use this package when you need one of these outcomes:

- export the active Bub tape store through a standard CLI path
- export any standard tape store from Python without tying the export to a specific backend
- shape exports with CEL before publishing or downstream consumption
- stage a dataset locally for scanning and review before uploading it elsewhere

## Stable Boundaries

This package keeps three boundaries explicit:

1. source boundary: the exporter reads only through the standard tape store interface
2. transport boundary: the exporter writes only through OpenDAL
3. data boundary: the output remains tape-native, with derived summaries and anchor slices layered on top

That means:

- SQLite, SQLAlchemy, Redis, or another tape-store implementation can be the source as long as it follows the standard interface
- local filesystem, S3, MinIO, GCS, AzBlob, and other OpenDAL targets are all just sinks
- the export format is not constrained by another application-specific session schema

## Bub CLI

The package exposes one Bub plugin entry point:

- plugin entry point: `tape-dataset-opendal`
- command: `bub tape-export`

Minimal example:

```bash
bub tape-export \
  --scheme fs \
  --config root=/tmp/tape-dataset \
  --root snapshot-2026-04-23
```

S3-compatible example:

```bash
bub tape-export \
  --scheme s3 \
  --config bucket=my-bucket \
  --config endpoint=http://127.0.0.1:9000 \
  --config region=us-east-1 \
  --config access_key_id=minioadmin \
  --config secret_access_key=minioadmin \
  --config enable_virtual_host_style=false \
  --root snapshot
```

Useful flags:

- `--filter`: repeatable CEL expression
- `--filter-file`: file containing one CEL expression per line
- `--no-segments`: skip `segments.jsonl`
- `--no-raw`: skip `raw/*.jsonl`

## Python API

### Sync

```python
from opendal import Operator
from republic.tape import InMemoryTapeStore
from republic.tape.entries import TapeEntry

from tape_dataset_opendal import ExportableTapeStore

inner = InMemoryTapeStore()
inner.append("ops__incident", TapeEntry.anchor("triage"))
inner.append("ops__incident", TapeEntry.message({"role": "user", "content": "DB timeout"}))

store = ExportableTapeStore(inner)
report = store.export_dataset(Operator("fs", root="/tmp/tape-dataset"))

print(report.entry_count)
print(report.manifest_path)
```

### Async

```python
import asyncio

import opendal
from republic.tape.entries import TapeEntry
from republic.tape.store import AsyncTapeStoreAdapter, InMemoryTapeStore

from tape_dataset_opendal import AsyncExportableTapeStore


async def main() -> None:
    inner = AsyncTapeStoreAdapter(InMemoryTapeStore())
    store = AsyncExportableTapeStore(inner)
    await store.append("agent__session", TapeEntry.anchor("task"))
    await store.append("agent__session", TapeEntry.message({"role": "assistant", "content": "Done"}))

    report = await store.export_dataset_async(
        opendal.AsyncOperator("fs", root="/tmp/tape-dataset"),
    )
    print(report.segment_count)


asyncio.run(main())
```

## CEL Filtering

Filtering uses `cel-python`. The package does not define a custom filter DSL.

```bash
bub tape-export \
  --scheme fs \
  --config root=/tmp/tape-dataset \
  --root filtered \
  --filter 'kind == "message"' \
  --filter 'payload.role == "user" || text.contains("timeout")'
```

When the filter set is large:

```bash
bub tape-export \
  --scheme fs \
  --config root=/tmp/tape-dataset \
  --root filtered \
  --filter-file ./filters.cel
```

Filter files use one expression per line. Lines beginning with `#` are ignored.

Available CEL variables per entry:

- `tape`
- `kind`
- `date`
- `payload`
- `meta`
- `text`
- `json`
- `entry`

Multiple filters are combined with logical `AND`.

## Output Layout

Each export writes:

- `manifest.json`: export metadata, counts, paths, and active filters
- `tapes.jsonl`: one summary record per tape
- `entries.jsonl`: one normalized record per retained entry
- `segments.jsonl`: anchor-derived slices when segment export is enabled
- `raw/<encoded-tape>.jsonl`: canonical tape-native JSONL per tape when raw export is enabled

Segment rules follow standard tape semantics:

- if a tape contains anchors, each segment starts at one anchor and ends before the next anchor
- if a tape contains no anchors, the whole tape is exported as one `full_tape` segment

## Release Workflow

For transport or integration testing, direct export to remote object storage is enough.

For public-sharing workflows, use this order:

1. export to local filesystem staging
2. apply CEL filters or manual edits
3. run TruffleHog on the staged output
4. run a final LLM review on the staged dataset
5. publish only after those checks pass

The skill at [src/skills/tape-dataset-share-review/SKILL.md](./src/skills/tape-dataset-share-review/SKILL.md) exists to guide that workflow.

## Verification

The package includes a focused verification recipe for the SQLite through SQLAlchemy to MinIO path:

- [docs/how-to-verify-sqlalchemy-sqlite-to-minio.md](./docs/how-to-verify-sqlalchemy-sqlite-to-minio.md)

That guide verifies two things together:

- tapes are generated through the real Bub runtime before export
- a real `bub-tapestore-sqlalchemy` SQLite source can be exported through the Bub CLI path to an S3-compatible backend
- one tapestore containing multiple tapes is exported as multiple dataset members

## License

Apache-2.0

## Acknowledgment

The release-review workflow in this package was informed in part by the ideas in
[`pi-share-hf`](https://github.com/badlogic/pi-share-hf).
