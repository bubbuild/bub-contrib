# bub-ag-ui

Connect a browser UI to Bub through [AG-UI](https://docs.ag-ui.com/). The adapter
accepts `RunAgentInput` at `POST /agent` and streams text, reasoning, tool calls,
application state, and human-input interrupts over SSE. Bub runs the agent and
keeps its conversation context; your application owns the displayed history.

## Start the server

Install this package in the environment that runs Bub. From this repository:

```sh
uv sync
uv run bub ag-ui --host 127.0.0.1 --port 28201
```

Use your normal Bub model configuration. Browser and deployment settings:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `BUB_AG_UI_ORIGINS` | `["http://localhost:3000"]` | JSON array of allowed browser origins |
| `BUB_AG_UI_TOKEN` | Unset | Bearer token; required for non-loopback access |
| `BUB_AG_UI_MAX_RUN_SECONDS` | `600` | Native turn timeout, including time waiting for the client |

When a token is configured, send `Authorization: Bearer <token>`. A shared token
represents one identity. For multiple users, supply a trusted `authenticate`
callback to `create_app`; a client-chosen thread ID is not authentication.

## Connect a frontend

Install `@ag-ui/client`, then create an agent:

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

Keep the same `threadId` for subsequent turns and preserve message IDs when
sending history. Use the agent's `headers` option for authentication.

`HttpAgent` maintains the client messages and state. Save these in your application
and pass them as `initialMessages` and `initialState` when restoring the UI.
Sending only new input can continue Bub's model context, but does not restore
missing display history. Responses update the client incrementally; a partial
request is not published as a replacement for the full conversation.

## Tools and human input

### Frontend tools

Declare frontend tools in the request's `tools` list, with a name, description,
and JSON parameter schema. Server tools execute in Bub; frontend tools execute
in your application or UI framework.

1. Consume the tool calls and wait for the HTTP run to finish.
2. Execute the frontend calls and add one result message for each: a new `id`,
   `role: "tool"`, the matching `toolCallId`, and `content`.
3. Run again on the same thread with all pending results, the current state, and
   unchanged tool definitions. No additional user message is needed.

Tool names must not collide with server tools or each other; `web.search` and
`web_search` share a model-facing name. Frontend tools belong to the parent turn,
not its child agents.

### Human input

Configure server-side `InterruptTool` definitions through `AGUIAdapter`'s
`interrupts` argument. Each definition supplies a name, description, parameter
schema, and an optional response schema.

A human-input pause ends with `RUN_FINISHED` carrying `outcome.type: "interrupt"`.
The streamed messages and a state snapshot provide the decision UI's context.
Respond to every open interrupt:

```ts
await agent.runAgent({
  resume: [{ interruptId, status: "resolved", payload: true }],
});
```

Use `status: "cancelled"` without a payload to abandon a decision. Resolved payloads
must match their response schemas. Missing, partial, duplicate, or invalid
responses produce `RUN_ERROR` and can be corrected and resubmitted. Use `resume`,
not ordinary tool-result messages, for interrupts.

## Sessions, limits, and errors

- **Run completion:** `RUN_FINISHED` ends the HTTP run. Bub may still be waiting
  for frontend results or human input; one native turn can span several requests.
- **Waiting calls:** resume on the same adapter before the turn timeout. Restart,
  shutdown, or timeout discards unfinished calls, even with a persistent tape store.
- **Conversation context:** completed context follows Bub's configured tape store.
  Echoed history does not replace it, and editing accepted user input is rejected.
  Start a new thread for a new conversation.
- **Cancellation:** `agent.abortRun()` cancels an active HTTP execution, not tool
  side effects already performed. It cannot cancel a waiting turn after that HTTP
  run has finished; resolve its calls, cancel its interrupts, or let it time out.
- **Concurrency and retries:** one executing request per thread, with no
  cross-process coordination. Repeated accepted interrupt responses are acknowledged
  without re-execution; this is not answer replay or general request deduplication.
- **Errors:** authentication, malformed bodies, and body-size limits use HTTP errors.
  Execution and continuation failures arrive as `RUN_ERROR`.

The adapter exposes no display-history, replay, or separate cancellation endpoint.

## Embedding and development

Use `bub_ag_ui.http.create_app` to embed the endpoint. Set `manage_framework=False`
when the host manages `framework.running()`. In a Bub gateway, set
`channel_managed=True`; an explicitly supplied adapter must also use
`bind_router=False`.

Run the tests from the repository root with Python 3.12+ and Node.js 22+:

```sh
uv sync --frozen --group test
uv run --frozen pytest packages/bub-ag-ui/tests
npm ci --prefix packages/bub-ag-ui/tests/client
npm test --prefix packages/bub-ag-ui/tests/client
```

Socket tests use the published AG-UI client and assistant-ui runtime with a
local deterministic provider; no model API key is needed. Set `PYTHON` if the
interpreter is not at `.venv/bin/python`.
