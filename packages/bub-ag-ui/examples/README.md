# Bub + AG-UI web example

This example pairs a CopilotKit chat frontend with the real Bub AG-UI gateway.
A small deterministic model plugin keeps the default walkthrough local and
repeatable, so no model-provider credential is required.

The complete path is:

```text
CopilotChat -> Copilot Runtime -> AG-UI HttpAgent -> bub-ag-ui -> Bub turn
CopilotChat <- Copilot Runtime <- AG-UI events    <- bub-ag-ui <- Bub output
```

The frontend is adapted from the original Apache-2.0 Bub template. Run the
commands below from the `bub-contrib` repository root.

## 1. Start the Bub gateway

```bash
BUB_STREAM_OUTPUT=true uv run --isolated --python 3.12 --no-project \
  --with 'bub @ git+https://github.com/bubbuild/bub.git' \
  --with-editable packages/bub-ag-ui \
  --with-editable packages/bub-ag-ui/examples/echo-plugin \
  bub gateway --enable-channel ag-ui
```

The temporary editable install exposes the example model through Bub's normal
`bub` entry-point group. It overrides only `run_model_stream`; the gateway,
channel routing, turn orchestration, streaming, and outbound dispatch all use
Bub's real runtime path.

Wait until Uvicorn is listening on `http://127.0.0.1:8088`.

## 2. Start the web frontend

In another terminal:

```bash
cd packages/bub-ag-ui/examples/frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`, enter a message, and submit it. The page talks to
the Copilot Runtime on port 4000, whose AG-UI `HttpAgent` forwards the run to
the Bub endpoint at `http://127.0.0.1:8088/agent`.

The response begins with `Bub received through AG-UI:`. Both the browser UI and
Bub gateway stream the same AG-UI run. Stop the frontend and gateway with
Ctrl-C when finished.

## Configuration

Copy `frontend/.env.example` to `frontend/.env` to override any endpoint or
port:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BUB_AG_UI_AGENT_URL` | `http://127.0.0.1:8088/agent` | Copilot Runtime → Bub |
| `COPILOTKIT_PORT` | `4000` | Copilot Runtime port |
| `VITE_COPILOTKIT_RUNTIME_PROXY` | `http://127.0.0.1:4000` | Vite → Copilot Runtime |
| `FRONTEND_PORT` | `5173` | Vite frontend port |

## Use a real model instead

Omit the example plugin and provide the normal Bub model settings:

```bash
export BUB_MODEL=openai:gpt-5-mini
export BUB_API_KEY=...
export BUB_STREAM_OUTPUT=true
uv run --isolated --python 3.12 --no-project \
  --with 'bub @ git+https://github.com/bubbuild/bub.git' \
  --with-editable packages/bub-ag-ui \
  bub gateway --enable-channel ag-ui
```

Keep the frontend running unchanged. Any provider supported by Bub can replace
the deterministic example model.
