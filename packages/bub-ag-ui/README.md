# bub-ag-ui

`bub-ag-ui` adds an [AG-UI](https://docs.ag-ui.com/) HTTP channel to the Bub gateway. It accepts AG-UI `RunAgentInput` requests, forwards them through Bub's normal channel pipeline, and translates Bub stream events into AG-UI server-sent events.

The package targets Bub 0.4.0's public extension contracts. Bub loads it as the `ag-ui` entry point, so the package intentionally does not declare Bub as a runtime dependency.

## Install

From a Bub project:

```bash
bub install bub-ag-ui@main
```

## Configure

The channel is available under the `ag-ui` key in Bub configuration:

```yaml
ag-ui:
  host: 127.0.0.1
  port: 8088
  path: /agent
  health_path: /agent/health
```

Environment variables with the `BUB_AG_UI_` prefix override these values:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BUB_AG_UI_HOST` | `127.0.0.1` | HTTP bind host |
| `BUB_AG_UI_PORT` | `8088` | HTTP bind port |
| `BUB_AG_UI_PATH` | `/agent` | AG-UI request endpoint |
| `BUB_AG_UI_HEALTH_PATH` | `/agent/health` | Health endpoint |

Set `BUB_STREAM_OUTPUT=true` to receive model output as live AG-UI text events. Without it, successful runs still complete over SSE, but output usually arrives as one final text block.

## Run

```bash
export BUB_STREAM_OUTPUT=true
bub gateway --enable-channel ag-ui
```

The default endpoints are:

- `POST /agent` for AG-UI runs
- `GET /agent/health` for channel health

For a runnable, credential-free walkthrough that starts the real Bub gateway
and sends a protocol-native AG-UI request, see the
[`examples`](examples/README.md) directory.

## Event mapping

| Bub stream event | AG-UI event |
| --- | --- |
| `text` | `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`, `TEXT_MESSAGE_END` |
| `tool_call` | `TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END` |
| `tool_result` | `TOOL_CALL_RESULT` |
| `usage` | `CUSTOM` named `bub.usage` |
| `error` | `RUN_ERROR` |

The channel emits `RUN_STARTED` before streamed work and `RUN_FINISHED` only after Bub successfully dispatches the final output. Public frontend state becomes normal Bub turn state; AG-UI transport metadata is preserved under the private `_ag_ui` key for downstream integrations.

## Development

From the `bub-contrib` repository root:

```bash
uv run --group test pytest -q packages/bub-ag-ui/tests
uv build --package bub-ag-ui
```

## Current limitations

- Session mapping is primarily aligned by AG-UI `thread_id`.
- Resume and interrupt semantics are not yet connected to Bub.
- The prompt fallback uses the last user message and plain scalar context values. Structured transport metadata remains under `_ag_ui`.
