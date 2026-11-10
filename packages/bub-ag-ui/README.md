# bub-ag-ui

Connect a web frontend to Bub through AG-UI HTTP/SSE. Stream text, reasoning,
tool calls, and shared state, with support for browser-side tools, human input,
and conversation history.

## Start the server

Install this package in the environment that runs Bub. In this repository it is
already a root dependency:

```sh
uv sync
uv run bub ag-ui --host 127.0.0.1 --port 28201
```

The server uses your normal Bub model/provider configuration and tape store.
History survives a server restart only when that store is persistent.

## Connect a frontend

Use an AG-UI client or a frontend integration that accepts an AG-UI endpoint.
For a minimal browser client, install `@ag-ui/client` and send a message:

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
```

Keep the `threadId` to continue or reload the same conversation. Use a new `runId`
for each new user turn or tool continuation; reuse a run ID only when retrying
that exact request. Your frontend is responsible for rendering streamed updates
and handling any browser-side tools.

### Authentication and browser origins

- Loopback access works without a token by default.
- For non-loopback listening, set `BUB_AG_UI_TOKEN` and send
  `Authorization: Bearer <token>` on requests. `HttpAgent` accepts a `headers`
  option for this.
- Set `BUB_AG_UI_ORIGINS` to a JSON array of allowed browser origins. The default
  is `["http://localhost:3000"]`.
- A shared token represents one identity, not separate users. For multi-user
  hosting, supply a trusted `authenticate` callback to `create_app` that returns
  a stable user identity. A client-chosen thread ID is not authentication.

## Tools and human input

| Tool type | What the frontend should expect |
| --- | --- |
| Server tool | Bub executes it and continues the response without client action. |
| Frontend tool | The run ends with a pending call. Execute it in the browser, then submit its result in a new run. |
| Human-input interrupt | The run ends with an interrupt outcome. Show the decision UI, then submit a `resume` entry in a new run. |

**`RUN_FINISHED` means that run has ended, not necessarily that the agent has
finished answering.** Check for pending tool calls or an interrupt outcome.

Declare frontend tools in the request's `tools` list. Return each result as a
message with `role: "tool"`, a new message `id`, the matching `toolCallId`, and
its `content`. Send the tool definitions again on the continuation. You do not
need to add a synthetic user message to make the agent continue.

Human-input tools are configured on the server with `InterruptTool`. Respond
using `resume`, with the matching `interruptId`, a status, and the response
payload. Resolved payloads must satisfy the interrupt's response schema, if any.

Results can arrive in separate requests, but the agent waits until all pending
calls are resolved before continuing. Resolve them before starting another user
turn. Unknown or duplicate results are rejected. Tool names must not collide
with server tools or each other; `web.search` and `web_search` count as the same
model-facing name.

Browser tools and interrupts belong to the parent AG-UI turn. Child agents cannot
directly use them; they can return a request to the parent agent to act on.

## History, retries, and cancellation

- **Reload a conversation:** fetch its history and restore your client's messages
  and state. The response also includes pending calls, run status, and a replay
  cursor. History restoration is a Bub extension; connecting a standard
  `HttpAgent` alone does not automatically fetch it.
- **Retry a request:** the same run ID and identical request join the existing run
  or replay its saved events, without executing the model or tools again. Reusing
  that ID for a different request returns HTTP `409`.
- **Replay events:** use the events endpoint with `after=<cursor>` or a
  `Last-Event-ID` header. Replay returns saved output and follows new output if
  the run is still active; it does not restart execution. Treat SSE IDs as cursors,
  not consecutive event counts.
- **Disconnect:** closing the response that started a run cancels that run.
  Closing a separate replay or duplicate-request connection does not cancel it.
  Reconnecting can recover saved output, but does not resume cancelled work.
- **Cancel explicitly:** use the cancel endpoint. Cancellation does not undo
  side effects of tools that have already executed.
- **Restart after an interrupted run:** the unfinished run is marked failed,
  rather than automatically executed again. Partial output remains available,
  but it does not imply a completed model exchange or successful tool result.

Stored history is authoritative: echoing modified assistant messages does not
rewrite it. To edit or regenerate, send a new run whose message list ends at the
existing user message being edited or retried. Bub uses that point as the start
of a new context branch. Resolve pending tools before editing.

Reset history only while the thread is idle. After a reset, reload history and
discard old replay cursors. Clearing display history alone does not clear the
model's conversation context; use a new thread for a separate conversation.

## HTTP endpoints and limits

All routes use the same authentication.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/agent` | Submit an AG-UI `RunAgentInput`; receive SSE events. |
| `GET` | `/threads/{thread_id}/history` | Restore messages, state, pending calls, run status, and cursor. |
| `GET` | `/threads/{thread_id}/runs/{run_id}/events` | Replay or follow a run's events. |
| `POST` | `/threads/{thread_id}/runs/{run_id}/cancel` | Cancel an active run; returns `{"cancelled": true}` if cancelled. |

The `/threads/...` routes are Bub extensions, not standard AG-UI endpoints.
An unknown run returns `404` on the replay and cancel routes.

Only one run may execute on a thread at a time; another run receives `409` while
that thread is busy. Run one adapter/controller per store/workspace; multiple
server processes sharing it are not supported. Long histories and additional
subscribers can increase event-reading overhead.

### Embedding and extensions

Use `bub_ag_ui.http.create_app` to embed the endpoint. In an existing Bub gateway,
set `channel_managed=True`; an explicitly supplied `AGUIAdapter` must also use
`bind_router=False`. Set `manage_framework=False` if the host already manages
`framework.running()`.

Bub's model configuration and native tool policies still apply. Client-supplied
state and context are application data, not overrides for server configuration.
If you supply a custom model provider, it remains responsible for recording its
model conversation, including recovery from partial exchanges. Displayed output
alone is not automatically converted into model history.

## Development

From the repository root, with Python 3.12+ and Node.js 22+:

```sh
uv sync --frozen --group test
uv run --frozen --group test pytest packages/bub-ag-ui/tests
npm ci --prefix packages/bub-ag-ui/tests/client
npm test --prefix packages/bub-ag-ui/tests/client
```

Python tests cover execution, tools, history, isolation, and cancellation. Socket
tests use the published `@ag-ui/client` and assistant-ui integration against a
local server with a deterministic model provider. They need no model API key.
Set `PYTHON` if the interpreter is not at the workspace's `.venv/bin/python`.
Both test layers run in repository CI. These are regression and SDK
interoperability tests, not TCK certification.
