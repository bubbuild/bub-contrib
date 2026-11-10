# bub-ag-ui

Connect a frontend to Bub through AG-UI. The adapter accepts `RunAgentInput` at
`POST /agent` and streams AG-UI events for text, reasoning, tools, shared state,
and human-input interrupts. It is a protocol adapter, not a conversation-storage
or run-management API.

## Start the server

Install this package in the environment that runs Bub. In this repository it is
already a root dependency:

```sh
uv sync
uv run bub ag-ui --host 127.0.0.1 --port 28201
```

Use your normal Bub model/provider configuration. For browser access, set
`BUB_AG_UI_ORIGINS` to a JSON array of allowed origins; the default is
`["http://localhost:3000"]`.

Loopback access works without a token by default. For non-loopback listening,
set `BUB_AG_UI_TOKEN` and send `Authorization: Bearer <token>`. For multi-user
hosting, supply a trusted `authenticate` callback to `create_app` that returns a
stable user identity. A shared token represents one identity; a client-chosen
thread ID is not authentication.

## Connect a frontend

Install `@ag-ui/client` in your frontend project:

```ts
import { HttpAgent } from "@ag-ui/client";

const agent = new HttpAgent({
  url: "http://127.0.0.1:28201/agent",
  threadId: crypto.randomUUID(),
  initialMessages: [
    { id: crypto.randomUUID(), role: "user", content: "Hello, Bub!" },
  ],
});

await agent.runAgent({ runId: crypto.randomUUID() });
console.log(agent.messages);

agent.addMessage({
  id: crypto.randomUUID(), role: "user", content: "Tell me more.",
});
await agent.runAgent({ runId: crypto.randomUUID() });
```

`HttpAgent` maintains messages and state and sends them on subsequent runs. Keep
one stable `threadId` for the conversation and use a new `runId` for each run.
The frontend renders the streamed updates. `HttpAgent` also accepts a `headers`
option for authentication.

Bub owns the conversation and chooses model context from its native session tape.
The adapter passes new user input to the native stream and resolves waiting tools
with client results. Bub records the complete model/tool exchanges.
Keep message IDs stable when echoing history: already accepted input is not
appended again, and echoed assistant messages or old tool results do not replace
Bub's records. An edit to an accepted user message is rejected; this adapter does
not implement edit/regenerate branches. Start a new thread for a new conversation.

Your application can save messages, shared state, and the thread ID to restore
its UI through `initialMessages` and `initialState`. This does not import that
snapshot into Bub or restore deleted server context. A persistent Bub tape store
preserves completed conversation context across server restarts, but cannot
restore an unfinished tool call. Keep any interrupt outcome needed by your
decision UI as well. There are no `/threads/...` history, replay, or cancel
endpoints in this adapter.

## Frontend tools

Server tools execute in Bub. Frontend tools are declared in the request's `tools`
list and execute in your application, either through your UI framework's tool
handler or your own handler.

When a run requests frontend tools:

1. Wait for that run to finish and execute the requested tools.
2. Add a message for each result, with a new `id`, `role: "tool"`, the matching
   `toolCallId`, and `content`.
3. Start a new run on the same thread, sending the results, shared state, and
   unchanged tool definitions. Include a result for every pending frontend call.
   This request attaches to the same live native stream; a result-only request
   does not need to echo the suspended exchange.

No synthetic user message is needed. **`RUN_FINISHED` ends one run; it may mean
that the agent is waiting for your frontend, not that it has finished answering.**

Tool names must not collide with server tools or each other; `web.search` and
`web_search` count as the same model-facing name. Frontend tools and interrupts
belong to the parent AG-UI turn. Child agents can ask the parent to use them, but
cannot directly suspend the parent's frontend interaction.

## Human input and resume

Configure human-input tools on the server with `InterruptTool`. A pause ends with
`RUN_FINISHED` carrying `outcome.type: "interrupt"` and the pending interrupts.
The adapter sends message and state snapshots before this event so the client
has the context needed to continue.

Start a new run on the same thread with the current messages and state, plus a
`resume` entry for **every** open interrupt:

```ts
await agent.runAgent({
  resume: [{ interruptId, status: "resolved", payload: true }],
});
```

Use `status: "cancelled"` without a payload when the user abandons a decision.
A resolved payload must match the declared response schema, if any. Missing,
partial, unknown, or invalid responses produce `RUN_ERROR`; they do not execute
the model. Do not send an interrupt response as an ordinary tool-result message.

External tools await client results inside the live native execution. Waiting
calls are process-local: resume them on the same adapter before its turn timeout
expires. A server restart, adapter shutdown, or timeout discards that waiting
execution; sending its results later produces `RUN_ERROR`, not a reconstructed run.
The default `max_run_seconds` is 600 seconds for the entire native turn, including
client waiting time. In a mixed tool batch, Bub emits and records results together
after all frontend and backend calls complete.

The adapter records consumed interrupt responses on the native tape. Repeating
the same accepted responses does not re-execute resumed work; it is not a replay
of the earlier generated answer. This receipt can survive a restart with a
persistent tape store, but does not make a pending execution restartable.

Each thread uses one native tape. Bub continues to own its records, session
settings, loaded-tool visibility, and handoff anchors. The adapter neither resets
that tape on each request nor stores a complete client-history copy per run.
Echoing older messages cannot bring them back into model context after a native
handoff.

## Errors and cancellation

- Protocol execution failures arrive as `RUN_ERROR`, not `RUN_FINISHED`.
  Authentication, malformed request bodies, and body-size limits use HTTP errors.
- `agent.abortRun()` aborts the HTTP request and cancels its execution. It does
  not undo side effects of tools that have already run. Once a paused HTTP run
  has finished, aborting that old request cannot cancel the waiting native turn;
  resolve its calls (or cancel its interrupts through `resume`) before timeout.
- Another executing request on the same thread is rejected with `RUN_ERROR`.
  Run one adapter per store/workspace; cross-process coordination is not provided.
- Reusing a run ID does not request replay or provide general request deduplication.
  A history-only request with no new input is rejected rather than re-executed.
  Reconnecting does not restart interrupted work. Retry policy belongs to the
  application; consider tool side effects before submitting work again.

Execution uses the framework's native `BubAgent.run_stream` path. Bub owns the
model loop, step limits, steering, subagent delivery, automatic overflow handoff,
and conversation recording. The adapter wraps the stream to translate events and
attach client tool results; it does not run its own model-step loop or fabricate
user messages. Native lifecycle events and typed stream chunks are handled by
scoped observers on the agent's EventBus. The stream bridge publishes chunks
without changing the source stream; observers are removed when execution ends. An AG-UI run ends at an HTTP request boundary, while one native
turn may span several requests. Optional AG-UI `STEP_*` events are not emitted;
Bub's native step/run events remain available to server hooks.

## Embedding and development

Use `bub_ag_ui.http.create_app` to embed the endpoint. In an existing Bub gateway,
set `channel_managed=True`; an explicitly supplied `AGUIAdapter` must also use
`bind_router=False`. Set `manage_framework=False` if the host already manages
`framework.running()`. Bub's native model and tool policies still apply;
client-supplied state/context are application data, not server configuration.

From the repository root, with Python 3.12+ and Node.js 22+:

```sh
uv sync --frozen --group test
uv run --frozen --group test pytest packages/bub-ag-ui/tests
npm ci --prefix packages/bub-ag-ui/tests/client
npm test --prefix packages/bub-ag-ui/tests/client
```

Tests cover observable protocol output, application tools and state, authentication,
cancellation, streaming backpressure, and conversation continuity. Socket tests exercise the published AG-UI client
and assistant-ui runtime with a deterministic model provider; no model API key
is needed. Set `PYTHON` if the interpreter is not at `.venv/bin/python`.
These are regression and interoperability tests, not TCK certification.
