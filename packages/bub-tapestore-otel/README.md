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

For local Phoenix:

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:6006/v1/traces \
OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf \
uv run bub run "hello"
```

Plugin settings:

| Variable | Default | Description |
| --- | --- | --- |
| `BUB_TAPESTORE_OTEL_ENABLED` | `true` | Wrap the active tape store. |
| `BUB_TAPESTORE_OTEL_SERVICE_NAME` | `bub` | Service name used by Logfire. |

OTLP exporter configuration stays on the standard OpenTelemetry environment
variables such as `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and
`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`.

The projection is tape-first but emits Phoenix-friendly GenAI spans:

- `bub.invoke_agent` root span with `openinference.span.kind=AGENT`
- `bub.llm.chat` child span with `openinference.span.kind=LLM`
- `bub.tool.<name>` child spans with `openinference.span.kind=TOOL`

Message content, system prompt, token usage, model/provider metadata, and tool
calls are derived from committed tape entries and exported using OTel GenAI and
OpenInference attribute names.
