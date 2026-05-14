# bub-tapestore-sqlalchemy

SQLAlchemy-backed tape store plugin for `bub`.

## What It Provides

- Bub plugin entry point: `tapestore-sqlalchemy`
- A `provide_tape_store` hook implementation backed by SQLAlchemy
- One store implementation that works with any SQLAlchemy-supported database URL
- Declarative models and SQLAlchemy-native query/update flows for Bub tape storage

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-tapestore-sqlalchemy"
```

You can also install it with Bub:

```bash
bub install bub-tapestore-sqlalchemy@main
```

## Configuration

The plugin reads environment variables with prefix `BUB_TAPESTORE_SQLALCHEMY_`:

- `BUB_TAPESTORE_SQLALCHEMY_URL` (optional): SQLAlchemy database URL
  - Default: `sqlite+pysqlite:///<BUB_HOME>/tapes.db`
- `BUB_TAPESTORE_SQLALCHEMY_ECHO` (optional, default: `false`)
- `BUB_TAPESTORE_SQLALCHEMY_CONNECT_ARGS` (optional): JSON object forwarded to
  `sqlalchemy.create_engine(connect_args=...)`. User-supplied keys override the
  built-in defaults. Useful for backends that need keyword-only arguments which
  cannot be expressed in the URL — for example Turso/libSQL auth tokens:

  ```bash
  export BUB_TAPESTORE_SQLALCHEMY_URL='sqlite+libsql://your-db.turso.io/?secure=true'
  export BUB_TAPESTORE_SQLALCHEMY_CONNECT_ARGS='{"auth_token": "<TURSO_AUTH_TOKEN>"}'
  ```

## Runtime Behavior

- The plugin overrides Bub's builtin file-based tape store through `provide_tape_store`.
- The default database is SQLite, but any SQLAlchemy URL can be used.
- Entry IDs remain monotonic per tape.
- Query behavior matches Bub tape queries:
  - `all()`
  - `after_anchor(...)`
  - `last_anchor()`
  - `between_anchors(...)`
  - `kinds(...)`
  - `limit(...)`

## SQLAlchemy Notes

- Uses SQLAlchemy declarative models for `tapes` and `tape_entries`
- Uses ORM sessions and `select()` / `delete()` / transactional updates
- Enables `PRAGMA foreign_keys = ON` automatically for SQLite connections
