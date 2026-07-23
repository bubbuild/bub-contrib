# Bub + AG-UI end-to-end example

This example runs the real Bub gateway and sends it a real AG-UI request. A
small deterministic model plugin keeps the example local and repeatable, so no
provider credentials are required.

The complete path is:

```text
client.py -> AG-UI HTTP/SSE -> bub-ag-ui -> Bub turn pipeline -> echo model
          <- AG-UI events  <- bub-ag-ui <- Bub outbound pipeline <-
```

Run all commands from the `bub-contrib` repository root.

## 1. Start Bub

```bash
BUB_STREAM_OUTPUT=true uv run --isolated --python 3.12 --no-project \
  --with 'bub @ git+https://github.com/bubbuild/bub.git' \
  --with-editable packages/bub-ag-ui \
  --with-editable packages/bub-ag-ui/examples/echo-plugin \
  bub gateway --enable-channel ag-ui
```

The temporary editable install makes the example model visible to Bub through
the normal `bub` entry-point group. It overrides only `run_model_stream`; the
gateway, channel routing, turn orchestration, and outbound dispatch all remain
Bub's real runtime path.

Wait until Uvicorn reports that it is listening on `http://127.0.0.1:8088`.

## 2. Send an AG-UI run

In another terminal:

```bash
uv run --package bub-ag-ui python \
  packages/bub-ag-ui/examples/client.py \
  "Explain what this request proves in one sentence."
```

The client constructs a protocol-native `RunAgentInput`, posts it to `/agent`,
and prints every decoded server-sent event. A successful run includes this
sequence:

```text
RUN_STARTED
TEXT_MESSAGE_START
TEXT_MESSAGE_CONTENT
TEXT_MESSAGE_END
RUN_FINISHED
```

The text content begins with `Bub received through AG-UI:`. Stop the gateway
with Ctrl-C when finished.

## Use a real model instead

To exercise the same path with Bub's configured model, omit the example plugin
and provide your normal Bub model settings:

```bash
export BUB_MODEL=openai:gpt-5-mini
export BUB_API_KEY=...
export BUB_STREAM_OUTPUT=true
uv run --isolated --python 3.12 --no-project \
  --with 'bub @ git+https://github.com/bubbuild/bub.git' \
  --with-editable packages/bub-ag-ui \
  bub gateway --enable-channel ag-ui
```

Then run the same client command. Any provider supported by Bub can be used;
the AG-UI client and channel configuration do not change.
