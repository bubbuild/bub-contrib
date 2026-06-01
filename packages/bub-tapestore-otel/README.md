# bub-tapestore-otel

`bub-tapestore-otel` wraps the active Bub tape store and projects committed tape
writes to OpenTelemetry through Logfire.

It is a transparent tape-store decorator:

```text
Bub -> OTelTapeStore -> active TapeStore
                    -> Logfire / OTLP
```

The real tape backend can still be the builtin file store or another contrib
store such as SQLite, SQLAlchemy, or Redis. This package observes `append` and
`reset` calls after the real store succeeds, then emits best-effort spans. Export
failures are swallowed so telemetry cannot break tape persistence.

## Configuration

For local Jaeger:

```bash
LOGFIRE_SEND_TO_LOGFIRE=false \
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:4318/v1/traces \
uv run bub run ",tape.info"
```

Plugin settings:

| Variable | Default | Description |
| --- | --- | --- |
| `BUB_TAPESTORE_OTEL_ENABLED` | `true` | Wrap the active tape store. |
| `BUB_TAPESTORE_OTEL_SERVICE_NAME` | `bub` | Service name used by Logfire. |
| `BUB_TAPESTORE_OTEL_SEND_TO_LOGFIRE` | `false` | Send to hosted Logfire in addition to OTLP env exporters. |
| `BUB_TAPESTORE_OTEL_FORCE_FLUSH` | `true` | Flush after each completed tape batch for streamable local observation. |
| `BUB_TAPESTORE_OTEL_SHUTDOWN_AFTER_FLUSH` | `true` | Shut down Logfire after each flush so short-lived `bub run` processes exit cleanly. |

The projection is tape-first: spans carry `bub.tape.name`,
`bub.tape.entry.kind`, and `bub.tape.entry.name`. Prompt and message content are
not exported by default.

For long-running `bub chat` or `bub gateway` processes, set
`BUB_TAPESTORE_OTEL_SHUTDOWN_AFTER_FLUSH=false` so later tape batches can keep
using the same Logfire runtime.
