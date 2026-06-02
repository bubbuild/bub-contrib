# bub-tapestore-otel

`bub-tapestore-otel` wraps the active Bub tape store and projects committed tape
writes to OpenTelemetry through the OTLP HTTP exporter.

It is a transparent tape-store decorator:

```text
Bub -> OTelTapeStore -> active TapeStore
                    -> OpenTelemetry / OTLP
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

For local Jaeger:

```bash
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:4318/v1/traces \
OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf \
uv run bub run "hello"
```

Plugin settings:

| Variable | Default | Description |
| --- | --- | --- |
| `BUB_TAPESTORE_OTEL_ENABLED` | `true` | Wrap the active tape store. |
| `BUB_TAPESTORE_OTEL_SERVICE_NAME` | `bub` | OpenTelemetry `service.name` resource value. |
| `BUB_TAPESTORE_OTEL_AGENT_NAME` | `bub` | OpenTelemetry `gen_ai.agent.name` value. |

OTLP exporter configuration stays on the standard OpenTelemetry environment
variables such as `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and
`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`.

The projection is tape-first and uses separate namespaces for separate
semantic sources:

- `gen_ai.*` attributes and span names follow the current OpenTelemetry GenAI
  semantic conventions.
- `bub.*` attributes describe Bub-specific runtime facts such as tape identity,
  tape entry boundaries, loop step number, step duration, and the runtime tool
  name Bub actually executed.
- `openinference.*`, `llm.*`, `input.*`, and `output.*` attributes are emitted
  as Phoenix/OpenInference compatibility attributes so Phoenix can classify and
  render Bub spans usefully. They are not OpenTelemetry semantic-convention
  attributes.

It emits these spans:

- `invoke_agent bub` root span with `gen_ai.operation.name=invoke_agent`,
  `gen_ai.agent.name=bub`, and `openinference.span.kind=AGENT`
- `bub.agent.step` framework span for each Bub loop turn, carrying custom
  `bub.agent.step` and `bub.agent.step.duration_ms` attributes
- `chat <model>` child span with `gen_ai.operation.name=chat` and
  `openinference.span.kind=LLM`
- `execute_tool <name>` child spans with `gen_ai.operation.name=execute_tool`,
  `gen_ai.tool.call.*`, `bub.tool.*`, and `openinference.span.kind=TOOL`

All spans include `gen_ai.conversation.id` for trace correlation. Message
content, system prompt, token usage, model/provider metadata, and tool calls are
derived from committed tape entries and exported using OTel GenAI and
OpenInference attribute names. Bub loop turns do not currently have a dedicated
OTel GenAI semantic-convention attribute, so step numbering stays in the
`bub.*` namespace.
