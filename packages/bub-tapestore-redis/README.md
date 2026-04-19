# bub-tapestore-redis

Redis-backed async tape store plugin for `bub`.

## What It Provides

- Bub plugin entry point: `tapestore-redis`
- A `provide_tape_store` hook implementation backed by Redis
- Exported `RedisTapeStore` class for direct use
- Query behavior aligned with `republic` tape queries

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-tapestore-redis"
```

You can also install it with Bub:

```bash
bub install bub-tapestore-redis@main
```

## Configuration

The plugin reads environment variables with prefix `BUB_TAPESTORE_REDIS_`:

- `BUB_TAPESTORE_REDIS_URL` (optional)
  - Default: `redis://localhost:6379/0`
  - Include authentication in the URL when Redis requires it:
    - Password only: `redis://:password@localhost:6379/0`
    - ACL username and password: `redis://username:password@localhost:6379/0`
    - TLS: `rediss://username:password@host:6379/0`
- `BUB_TAPESTORE_REDIS_KEY_PREFIX` (optional)
  - Default: `republic:tape`

If the password contains reserved URL characters such as `@`, `:`, or `/`, it
must be URL-encoded before putting it in `BUB_TAPESTORE_REDIS_URL`.

## Runtime Behavior

- The plugin overrides Bub's builtin file-based tape store through `provide_tape_store`.
- The store keeps per-tape entry IDs monotonic with Redis-side atomic allocation.
- Anchor queries use a Redis sorted set so `after_anchor`, `last_anchor`, and
  `between_anchors` stay consistent with Bub/Republic expectations.
- The package exposes an async `RedisTapeStore` for non-plugin usage.

## Redis Notes

- Callers own the Redis client lifecycle when using the store classes directly.
- Keys with the same prefix land in the same Redis slot, which avoids `CROSSSLOT`
  errors for Redis multi-key operations.
- Tape names are losslessly encoded inside the key namespace, so names containing
  `{` or `}` do not collide.
