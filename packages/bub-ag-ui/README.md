# bub-ag-ui

Expose Bub agents through AG-UI HTTP/SSE, with streamed text, reasoning, tools,
shared state, interrupts, and persistent display history.

## Run

Install this package in the environment that runs Bub. In this workspace it is
already a root dependency and registers the `ag-ui` channel and CLI command:

```sh
uv sync
uv run bub ag-ui --host 127.0.0.1 --port 28201
```

Use your normal Bub model/provider configuration. Connect an AG-UI `HttpAgent` to
`http://127.0.0.1:28201/agent`. For non-loopback listening, set `BUB_AG_UI_TOKEN`
and send it as `Authorization: Bearer <token>`. For multi-user hosting, inject a
trusted authentication callback into `create_app`; a shared token represents one
principal, not multiple isolated users. Configure allowed browser origins with
`BUB_AG_UI_ORIGINS` (a JSON array; defaults to `["http://localhost:3000"]`).

The adapter also exposes authenticated `/threads/{thread_id}/history`,
`/threads/{thread_id}/runs/{run_id}/events`, and
`/threads/{thread_id}/runs/{run_id}/cancel` routes for history, replay, and
cancellation. These are Bub extensions, not standard AG-UI endpoints. Use one
adapter/controller per store/workspace; this is not a distributed multi-writer
run scheduler.

## Execution boundary

AG-UI and ACP remain separate plugins, but use the same framework turn boundary:

```text
HTTP RunAgentInput
    -> AGUIAdapter: validate, admit, persist accepted input
    -> framework.process_inbound(AGUIInboundMessage, stream_output=True)
         -> load_state / build_prompt hooks
         -> scoped AG-UI run_model_stream hook
              -> public agent.session / native agent.run_once steps
              -> EventBus: StepStarted / StepFinished
              -> native LLM/tool hooks and protocol tool suspension
         -> save_state / render_outbound / dispatch_outbound hooks
    -> checkpoint pending work and commit RUN_FINISHED (or RUN_ERROR)
```

`execution.py` owns the scoped hooks and stream routing. `runtime.py` owns protocol
admission, tape recovery, and cancellation. `runner.py` orchestrates the protocol
turn using native `BubAgent.run_once` steps: Bub still owns model requests, tool
policy hooks, and execution of each complete tool batch. It is not a replacement
agent class or a claim that AG-UI uses the CLI's continuous run loop.
The adapter does not override `Agent.start` or `BubAgent.run_stream`, patch Bub,
or depend on `bub-acp-server`.

Accepted AG-UI messages already live on the model tape. The execution hook runs
against that history without appending the framework's descriptive prompt again.
This also permits a result-only continuation without a synthetic user message.
LLM request customization uses the native `before_llm_call` hook/EventBus path;
`build_prompt` is not a second source of model history for this protocol.
Frontend tools still suspend at the protocol boundary and resume in a later run.
Frontend and interrupt tools belong to that AG-UI turn: nested native runs do not
receive them in their tool scope, and direct calls from another turn fail as
ordinary tool errors rather than suspending the parent. Child agents can return
a request to the parent, which decides whether to call a frontend tool.

Standalone hosting binds an AG-UI stream router. Gateway hosting uses
`AGUIChannel.stream_events` and keeps the gateway router intact. When embedding
in an existing gateway, use `create_app(..., channel_managed=True)`; an explicitly
injected adapter must likewise be constructed with `bind_router=False`.
`manage_framework` separately controls ownership of `framework.running()`.
Routing is attached to each inbound message, not a global current-run variable.

Framework save/render/dispatch hooks finish before protocol success is committed.
Cancellation closes the native stream and merges its tape fork before protocol cleanup;
step EventBus subscriptions are removed when the stream closes. Duplicate runs
and partial frontend results that do not execute a model do not start a new
framework turn.

The responsibilities have a single owner at each boundary:

| Responsibility | Owner |
| --- | --- |
| HTTP authentication, SSE encoding, disconnect policy | `http.py` |
| Admission, active task, protocol terminal outcome, protocol recovery | `runtime.py` |
| Framework integration and per-message stream routing | `execution.py` |
| Native step orchestration and return-to-client boundary | `runner.py` |
| Wire event adaptation and display reduction | `events.py` |
| Durable protocol events and derived history | Mounted `AGUIHistory` sidecar |
| Model exchange recording | The executing provider; native Bub steps use the native recorder |

Step completion comes from the native EventBus; recovery only repairs boundaries
left open by a failed or interrupted run. The routing layer does not independently
close steps. Stream adaptation does not infer a provider's persistence policy:
AG-UI does not listen for `ChatFinished` to detect missing records, buffer model
output for a second writer, or supplement a third-party provider's model tape.
A provider that delegates to the native agent retains native recording; a provider
that only emits events is not silently given a persistence policy by AG-UI.
This also applies to failure, cancellation, and restart: recovery closes open
protocol boundaries, clears pending frontend work, and records `RUN_ERROR`. It
neither synthesizes missing tool results nor rebuilds model context from display
history. Partial output remains visible and replayable; the next model turn uses
only context committed by the provider and explicitly accepted input. Providers
own any repair needed for their partially committed model exchanges.
Standalone and gateway hosting select different transport adapters but share the
same per-message stream function and HTTP lifecycle implementation.

## History, replay, and ownership

Stored messages are authoritative. Client-echoed history is not merged back or
compared field by field; only edits to existing user messages can create an edit
branch. New user input, pending tool results, and interrupt responses still pass
admission checks. Tool name collisions are checked by `ExternalTools`, including
again after hooks bind session tools. Before persisting application JSON patches,
the sidecar checks that they apply; ordinary events do not trigger a trial replay
of the entire history.

History is an `ag-ui` sidecar registered through `provide_tape_sidecar`, just like
Bub's spill storage. The canonical owner tape holds ordinary `ag_ui.admit`,
`ag_ui.checkpoint`, and `ag_ui.event` entries on that sidecar. The history response
is derived from those entries; there is no independent journal or persistent
projection index. Protocol writes go directly to the canonical tape, not into the
model's temporary fork, so subscribers see only durable output. Event followers
read run metadata and new wire records without reconstructing display history.
The pinned Bub `TapeQuery` has no entry-ID range query, so each follow read still
scans the sidecar; removing that remaining cost requires native store/query
support, not a parallel event journal in this plugin.

SSE IDs are native sidecar entry IDs. They may have gaps because admission and
checkpoint entries are not SSE events. Replay reads existing events and never
runs tools or the model again. This follows the same separation as
[Bub Walkman's snapshot/follow design](https://github.com/bubbuild/bub-walkman/blob/f3d842972de330f444536e9cea746e9ac61fc985/src/bub_walkman/sources.py):
reading a tape is separate from owning a producer. Walkman itself is a viewer,
not an agent execution scheduler; it is not a runtime dependency here.

A short, per-thread admission gate serializes duplicate detection and task
creation. It is not held while the model runs. Active tasks determine execution
ownership; replay subscribers hold no execution lock. Concurrent retries of the
same request join the existing run, and only the response that admitted the run
cancels it on disconnect. An independent run on a busy thread receives HTTP 409.
Cancellation settles pending writes and native tape cleanup before another run
can be admitted.

Bub's normal sidecar lifecycle applies: handoff retains display history, reset
clears it unless `keep_sidecars=("ag-ui",)` is supplied, and fork resets take
effect on merge. Reset only while the thread is idle. Resetting just the sidecar
forgets display history and idempotency records, not the model tape. Cursors are
valid only for the current sidecar lifetime; after a reset, reload history rather
than reusing old cursors. Old run replay requests return not-found once their
records are gone. No cross-process lease or multi-writer guarantee is provided.

Frontend and interrupt tools are ordinary `@tool` handlers with runtime-supplied
JSON schemas. Native deny/replace policies prevent suspension; admitted handlers
return control to the client only after the native parallel batch has settled.

## Execution API boundary

`runner.py` uses the public `BubAgent.run_once` API for model calls, tool batches,
and policy hooks. The outer loop still prepares tools and prompts, emits native
lifecycle events, handles queued messages and context overflow, and stops at the
frontend-tool boundary. These overlap with Bub's native run loop, but the current
public `run_on(tape, prompt)` API does not expose a resume-from-recorded-input or
external-tool suspension contract. Passing an empty prompt is not equivalent to
`run_once(None)`; accepted input is already on the model tape.

Replacing this loop requires those capabilities in Bub's public API. This package
does not override the agent, call its private loop, or patch the installed dependency.
History/replay, request idempotency, and edit branches are adapter service features,
not requirements imposed by AG-UI or functionality supplied by its Python SDK.
An explicit edit/regenerate request creates a new context branch from the selected
server-side history; failure recovery never implicitly performs that operation.

## SDK reuse and test scope

- `ag-ui-protocol` owns request/event models and validation; its `EventEncoder`
  owns SSE encoding. The adapter does not maintain a second protocol schema.
- Python tests cover Bub-specific behavior: execution hooks, persistence ownership,
  pending tools, request isolation, replay, cancellation, and HTTP admission.
- Socket tests use the published `@ag-ui/client` and assistant-ui against this
  adapter with a deterministic model transport. They exercise the SDK pipeline,
  reject compatibility warnings, and compare display history with the official
  client's reducer. This is **interoperability testing**, not TCK certification.

The upstream [conformance suite](https://github.com/ag-ui-protocol/ag-ui/blob/dbfbddb7b3b898dc120b90610c941173b982757f/spec/1.0/conformance/README.md)
tests first-party **clients** and explicitly excludes producer correctness. It is
not a server TCK that can be pointed at a Bub endpoint. Run it in an upstream
checkout with its workspace dependencies installed, using its own runner:

```sh
pnpm -C sdks/typescript/packages/client exec vitest run src/conformance
```

That suite is not part of Bub CI and was not run for this adapter. We do not copy
its fixtures, maintain a partial fixture runner, or count passing upstream client
tests as evidence that Bub is conformant. Server behavior remains covered by the
adapter's own regression and SDK interoperability tests above.

## Test

From the repository root, with Python 3.12+ and Node.js 22+:

```sh
uv sync --frozen --group test
uv run --frozen --group test pytest packages/bub-ag-ui/tests
npm ci --prefix packages/bub-ag-ui/tests/client
npm test --prefix packages/bub-ag-ui/tests/client
```

The socket tests launch a deterministic local Bub server and mock only its model
provider. They do not require an API key or contact a model service. Set `PYTHON`
to an alternative interpreter if it is not installed at the workspace's
`.venv/bin/python`. Both test layers run in repository CI.
