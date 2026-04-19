# bub-tapestore-sqlite

SQLite-backed tape store plugin for `bub`.

## What It Provides

- Bub plugin entry point: `tapestore-sqlite`
- A `provide_tape_store` hook implementation backed by SQLite
- One database file for all tapes instead of one JSONL file per tape
- Native SQLite indexes for tape listing, anchor lookup, and kind filtering

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-tapestore-sqlite"
```

You can also install it with Bub:

```bash
bub install bub-tapestore-sqlite@main
```

> Note:
> `bub-tapestore-sqlite` currently supports `bub` `0.3.1` and later.

## Configuration

The plugin reads environment variables with prefix `BUB_SQLITE_`:

- `BUB_SQLITE_PATH` (optional): database path
  - Default: `<BUB_HOME>/tapes.sqlite3`
- `BUB_SQLITE_BUSY_TIMEOUT_MS` (optional, default: `5000`)
- `BUB_SQLITE_JOURNAL_MODE` (optional, default: `WAL`)
- `BUB_SQLITE_SYNCHRONOUS` (optional, default: `NORMAL`)
- `BUB_SQLITE_EMBEDDING_MODEL` (optional): any-llm model identifier, for example `openai:text-embedding-3-small`

## Runtime Behavior

- The plugin overrides Bub's builtin file-based tape store through `provide_tape_store`.
- Default database location: `<BUB_HOME>/tapes.sqlite3`.
- Entry IDs come from the autoincrement `tape_entries.id` column.
- Query behavior matches Bub tape queries:
  - `all()`
  - `after_anchor(...)`
  - `last_anchor()`
  - `between_anchors(...)`
  - `kinds(...)`
  - `limit(...)`
- Optional vector search:
  - Message entries are indexed lazily when you call `await TapeQuery(tape, store).query("...").all()`.
  - Configure `BUB_SQLITE_EMBEDDING_MODEL`.
  - The first successful embedding call persists vector dimensions automatically from the returned embedding size.
  - Semantic queries reuse the standard `fetch_all()` path and still honor `kinds(...)`, `limit(...)`, anchor bounds, and date bounds.

## SQLite Notes

- Enables `PRAGMA foreign_keys = ON`
- Uses `PRAGMA journal_mode = WAL` by default
- Uses `PRAGMA synchronous = NORMAL` by default
- Applies `PRAGMA busy_timeout` for concurrent readers/writers in one process
- Loads the bundled `sqlite-vec` extension when vector search is enabled
