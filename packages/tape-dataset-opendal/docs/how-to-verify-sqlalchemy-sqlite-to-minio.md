# Verify SQLite Through SQLAlchemy to MinIO

Verification target:

- source: `bub-tapestore-sqlalchemy` backed by SQLite
- tape generation: real `bub run`
- export path: real `bub tape-export`
- sink: MinIO as an S3-compatible OpenDAL backend
- topology: one tapestore with multiple tapes

## Assumptions

Run these commands from the repository root.

The commands below use shell variables so the guide stays independent of any specific local directory layout.

## Prerequisites

- local MinIO at `http://127.0.0.1:9000`
- bucket `tape-export-test`
- credentials `minioadmin:minioadmin`
- `mc`
- `podman` or `docker`
- the `bub` source tree in this repository
- `bub-tapestore-sqlalchemy` in `bub-contrib/packages/`
- `tape-dataset-opendal` in `bub-contrib/packages/`

## 1. Set up reusable paths

```bash
export REPO_ROOT="$PWD"
export VERIFY_ROOT="$(mktemp -d /tmp/tape-dataset-opendal-verify.XXXXXX)"
export DB_PATH="$VERIFY_ROOT/tapes.db"
export STAGING_ROOT="$VERIFY_ROOT/staging"
export REPORT_PATH="$VERIFY_ROOT/candidate.trufflehog.json"
export EXPORT_PREFIX="exports/verify-sqlalchemy-sqlite"
export MINIO_ALIAS="minio"
```

Configure the MinIO alias if needed:

```bash
mc alias set "$MINIO_ALIAS" http://127.0.0.1:9000 minioadmin minioadmin
mc mb --ignore-existing "$MINIO_ALIAS"/tape-export-test
```

## 2. Generate two tapes

```bash
BUB_TAPESTORE_SQLALCHEMY_URL="sqlite+pysqlite:///$DB_PATH" \
uv run --directory ./bub \
  --with "$REPO_ROOT/bub-contrib/packages/bub-tapestore-sqlalchemy" \
  --with "$REPO_ROOT/bub-contrib/packages/tape-dataset-opendal" \
  bub run ",tape.handoff name=triage summary='db incident'" --session-id ops:alpha
```

```bash
BUB_TAPESTORE_SQLALCHEMY_URL="sqlite+pysqlite:///$DB_PATH" \
uv run --directory ./bub \
  --with "$REPO_ROOT/bub-contrib/packages/bub-tapestore-sqlalchemy" \
  --with "$REPO_ROOT/bub-contrib/packages/tape-dataset-opendal" \
  bub run ",tape.info" --session-id ops:alpha
```

```bash
BUB_TAPESTORE_SQLALCHEMY_URL="sqlite+pysqlite:///$DB_PATH" \
uv run --directory ./bub \
  --with "$REPO_ROOT/bub-contrib/packages/bub-tapestore-sqlalchemy" \
  --with "$REPO_ROOT/bub-contrib/packages/tape-dataset-opendal" \
  bub run ",tape.handoff name=triage summary='api incident'" --session-id ops:beta
```

```bash
BUB_TAPESTORE_SQLALCHEMY_URL="sqlite+pysqlite:///$DB_PATH" \
uv run --directory ./bub \
  --with "$REPO_ROOT/bub-contrib/packages/bub-tapestore-sqlalchemy" \
  --with "$REPO_ROOT/bub-contrib/packages/tape-dataset-opendal" \
  bub run ",tape.anchors" --session-id ops:beta
```

Verify the SQLite store:

```bash
uv run --directory ./bub \
  --with "$REPO_ROOT/bub-contrib/packages/bub-tapestore-sqlalchemy" \
  python - <<'PY'
import os
from collections import Counter

from bub_tapestore_sqlalchemy.store import SQLAlchemyTapeStore
from republic.tape.query import TapeQuery

store = SQLAlchemyTapeStore(f"sqlite+pysqlite:///{os.environ['DB_PATH']}")
tapes = sorted(store.list_tapes())
assert len(tapes) == 2, tapes

for tape in tapes:
    entries = list(TapeQuery(tape=tape, store=store).all())
    kinds = Counter(entry.kind for entry in entries)
    assert kinds == {"anchor": 2, "event": 4}, (tape, kinds)

print("runtime-generated tapes verified")
PY
```

## 3. Verify CLI plugin loading

```bash
uv run --directory ./bub \
  --with "$REPO_ROOT/bub-contrib/packages/bub-tapestore-sqlalchemy" \
  --with "$REPO_ROOT/bub-contrib/packages/tape-dataset-opendal" \
  bub --help
```

Confirm that `tape-export` appears in the command list, then inspect the active hook implementations:

```bash
uv run --directory ./bub \
  --with "$REPO_ROOT/bub-contrib/packages/bub-tapestore-sqlalchemy" \
  --with "$REPO_ROOT/bub-contrib/packages/tape-dataset-opendal" \
  bub hooks
```

Required hook lines:

- `provide_tape_store: builtin, tapestore-sqlalchemy`
- `register_cli_commands: builtin, tape-dataset-opendal`

## 4. Export to MinIO

```bash
BUB_TAPESTORE_SQLALCHEMY_URL="sqlite+pysqlite:///$DB_PATH" \
uv run --directory ./bub \
  --with "$REPO_ROOT/bub-contrib/packages/bub-tapestore-sqlalchemy" \
  --with "$REPO_ROOT/bub-contrib/packages/tape-dataset-opendal" \
  bub tape-export \
  --scheme s3 \
  --config bucket=tape-export-test \
  --config endpoint=http://127.0.0.1:9000 \
  --config region=us-east-1 \
  --config access_key_id=minioadmin \
  --config secret_access_key=minioadmin \
  --config enable_virtual_host_style=false \
  --root "$EXPORT_PREFIX"
```

Expected JSON fields:

- `tape_count = 2`
- `entry_count = 12`
- `segment_count = 4`

## 5. Verify remote dataset shape

```bash
mc ls --recursive "$MINIO_ALIAS"/tape-export-test/"$EXPORT_PREFIX"
```

```bash
python - <<'PY'
import json
import os
import subprocess

minio_alias = os.environ["MINIO_ALIAS"]
export_prefix = os.environ["EXPORT_PREFIX"]


def mc_cat(path: str) -> str:
    return subprocess.check_output(["mc", "cat", path], text=True)


base = f"{minio_alias}/tape-export-test/{export_prefix}"
manifest = json.loads(mc_cat(f"{base}/manifest.json"))
assert manifest["format"] == "tape.dataset", manifest
assert manifest["tape_count"] == 2, manifest
assert manifest["entry_count"] == 12, manifest
assert manifest["segment_count"] == 4, manifest

tape_rows = [
    json.loads(line)
    for line in mc_cat(f"{base}/tapes.jsonl").splitlines()
    if line.strip()
]
assert len(tape_rows) == 2, tape_rows
assert [row["entry_count"] for row in tape_rows] == [6, 6], tape_rows
assert [row["anchor_count"] for row in tape_rows] == [2, 2], tape_rows
assert [row["segment_count"] for row in tape_rows] == [2, 2], tape_rows

segment_rows = [
    json.loads(line)
    for line in mc_cat(f"{base}/segments.jsonl").splitlines()
    if line.strip()
]
assert len(segment_rows) == 4, segment_rows
assert len({row["tape"] for row in segment_rows}) == 2, segment_rows
assert {row["segment_kind"] for row in segment_rows} == {"anchor_slice"}, segment_rows

print("remote dataset verified")
PY
```

## 6. Export to local staging

```bash
mkdir -p "$STAGING_ROOT"

BUB_TAPESTORE_SQLALCHEMY_URL="sqlite+pysqlite:///$DB_PATH" \
uv run --directory ./bub \
  --with "$REPO_ROOT/bub-contrib/packages/bub-tapestore-sqlalchemy" \
  --with "$REPO_ROOT/bub-contrib/packages/tape-dataset-opendal" \
  bub tape-export \
  --scheme fs \
  --config root="$STAGING_ROOT" \
  --root candidate
```

```bash
python - <<'PY'
import json
import os
from pathlib import Path

base = Path(os.environ["STAGING_ROOT"]) / "candidate"

manifest = json.loads((base / "manifest.json").read_text())
assert manifest["format"] == "tape.dataset", manifest
assert manifest["tape_count"] == 2, manifest
assert manifest["entry_count"] == 12, manifest
assert manifest["segment_count"] == 4, manifest

tape_rows = [
    json.loads(line)
    for line in (base / "tapes.jsonl").read_text().splitlines()
    if line.strip()
]
assert len(tape_rows) == 2, tape_rows
assert [row["entry_count"] for row in tape_rows] == [6, 6], tape_rows

segment_rows = [
    json.loads(line)
    for line in (base / "segments.jsonl").read_text().splitlines()
    if line.strip()
]
assert len(segment_rows) == 4, segment_rows

print("staged dataset verified")
PY
```

## 7. Scan the staged export

```bash
python ./bub-contrib/packages/tape-dataset-opendal/src/skills/tape-dataset-share-review/scripts/trufflehog_scan.py \
  "$STAGING_ROOT/candidate" \
  --report "$REPORT_PATH"
```

```bash
python - <<'PY'
import json
import os
from pathlib import Path

report = json.loads(Path(os.environ["REPORT_PATH"]).read_text())
assert report["blocking"] is False, report
assert report["summary"]["findings"] == 0, report
print("staged dataset scan verified")
PY
```

## 8. Final review

Hand the staged directory to the share-review skill or another project-aware review process before publication.

Skill entry:

- [src/skills/tape-dataset-share-review/SKILL.md](../src/skills/tape-dataset-share-review/SKILL.md)

## Result

If every command and assertion above succeeds, this path is verified.
