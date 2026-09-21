# bub-acp-server

Expose Bub as an Agent Client Protocol agent.

## What It Provides

- Bub plugin entry point: `acp-server`
- CLI command registered on Bub: `bub acp`
- Optional Streamable HTTP and WebSocket transports at `/acp`, served by Hypercorn with HTTP/2 support
- Gateway channel: `acp-server`, with HTTP startup and shutdown managed by Bub
- ACP agent methods for `initialize`, `session/new`, `session/load`, `session/resume`, `session/list`, `session/close`, and `session/prompt`
- Streaming ACP `session/update` events from Bub stream events
- ACP client-backed replacements for Bub's `bash`, `fs.read`, `fs.write`, and `fs.edit` tools while the ACP server is running
- An ACP-aware `update_plan` tool that updates the client plan UI and records each complete plan as a `plan` event in the session tape
- Automatic recovery of the latest persisted plan into the next ACP turn's model context
- Session-scoped model and reasoning-effort selection through ACP config options
- Session MCP servers over stdio, Streamable HTTP, and SSE via `bub-mcp`
- ACP context-compaction notifications when `tape.handoff` runs
- Mid-turn steering through the `_lody/session/steer` ACP extension

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-acp-server"
```

Or from a Bub project:

```bash
bub install bub-acp-server@main
```

## Usage

Configure an ACP-compatible client to launch:

```bash
bub acp
```

The previous `bub acp serve` form remains accepted temporarily and prints a deprecation warning. Other positional arguments are rejected.

The process speaks ACP over stdio. Prompts are sent through Bub's hook pipeline with stream output enabled, so model chunks and tool events can be displayed by the ACP client as they arrive.

The replacement `bash` tool accepts an optional `title` parameter for the ACP tool call display, for example `{"cmd": "git status --short", "title": "Check working tree changes"}`. If `title` is omitted or blank, the command is displayed as the title.

Bash tool call content shows the command prefixed with `$ ` before the terminal output. Loaded session history preserves the same command-then-output order.

The agent sends an ACP `usage_update` whenever the streamed usage snapshot changes, with a final end-of-stream check as a fallback. Missing token usage is reported as `0`. If the model provider does not report its context-window size, set `BUB_ACP_SERVER_CONTEXT_WINDOW_SIZE`; the default is `128000` tokens.

ACP clients can select both the model and reasoning effort for each session. Reasoning effort defaults to `auto`; the selected value is persisted with the ACP session and passed into Bub's turn state for subsequent model calls.

Available reasoning efforts are `auto`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`. Values are passed through to the model provider; actual support depends on the selected model and provider.

The ACP stream router reports Bub's built-in `tape.handoff` as a context-compaction tool call. Compatible clients receive `Context compacting` and `Context compacted` updates marked with `_meta.contextCompaction`.

Bub keeps using its own configuration, tools, skills, and tapes. The ACP client starts the process and displays the session; it does not replace Bub's model setup.

Each ACP connection owns a Bub `Agent` for each session, with client filesystem, terminal, and plan tools. The agent
is passed through the inbound message's `_runtime_agent` field so tape recovery, tool descriptions,
and execution use the same instance. Client tools never modify the global `REGISTRY`, and other
agents keep their own tools. This requires Bub's instance-tool API introduced in upstream PR #311.

ACP session IDs remain the protocol-facing `chat_id`. Bub namespaces its internal session ID with the ACP channel before selecting a tape, so an equal session ID from another channel cannot reuse the ACP tape.

ACP session metadata is stored under Bub home as `acp-sessions.json` so compatible clients can list sessions again after restarting. Keep `BUB_HOME` stable if you want the same ACP thread list across editor launches.

`bub-acp-server` supports both ACP session load and resume. `session/load` restores the matching Bub history through the same ACP streaming path used by live turns. `session/resume` attaches the editor back to the Bub session without replaying history, so later turns keep streaming through Bub's normal hook pipeline.

History is read through Bub's configured tape store. Store errors are reported instead of falling back to a separate local JSONL reader.

## Session MCP servers

`bub-acp-server` depends on `bub-mcp>=0.3.0`. Initialization advertises
`mcpCapabilities: {"http": true, "sse": true}`; stdio support is implicit in ACP.
Pass servers in `mcpServers` when creating, loading, or resuming a session:

```json
{
  "cwd": "/path/to/workspace",
  "mcpServers": [
    {"name": "local", "command": "/path/to/mcp-server", "args": [], "env": []},
    {"type": "http", "name": "remote", "url": "https://example.com/mcp", "headers": []}
  ]
}
```

Stdio processes use the session's working directory and supplied environment variables. HTTP/SSE
connections receive the supplied headers. Setup waits for tool discovery; a connection failure
returns an ACP error and closes the newly opened connections, preserving any previous session
connections. Tools use bub-mcp names such as `mcp.remote_search` and are available only to that
session's Agent. Session tools take precedence over configured MCP tools with the same name.

Loading or resuming replaces the entire server list; an omitted or empty list clears it. Active
prompts finish before their connections are replaced. Closing a session cancels its pending turns
and closes its MCP connections. Stdio, HTTP, and WebSocket server shutdown also releases all MCP
connections. HTTP connections share the active session resources, so reconnecting clients can
replace them without leaving the old connections running.

MCP server configuration, environment values, and headers are kept in memory and are not written
to `acp-sessions.json` or `mcp.json`. Clients must resend configuration after a server restart.

## HTTP and WebSocket transports

HTTP uses the SDK's experimental [web transport](https://agentclientprotocol.github.io/python-sdk/web-transport/), including POST requests, SSE streams, and connection IDs. Install the optional dependencies from a checkout:

```bash
uv pip install -e 'packages/bub-acp-server[http]'
```

Start the HTTP channel with the gateway:

```bash
bub gateway --enable-channel acp-server
```

Or set `BUB_ENABLED_CHANNELS=acp-server` and run `bub gateway`. The ACP channel is an `Interface`: it must be explicitly enabled and is not started by `all`. Other channels can be enabled alongside it with additional `--enable-channel` options. Gateway owns the framework/tape-store lifetime and stops the HTTP server and its background steering tasks on shutdown.

Gateway settings use the `BUB_ACP_SERVER_` prefix:

- `HOST`: listen address, default `127.0.0.1`
- `PORT`: listen port, default `28200`
- `CERTFILE` / `KEYFILE`: optional TLS certificate and private key; provide both

The same fields can be configured under `acp-server` in Bub's YAML configuration:

```yaml
enabled_channels: acp-server
acp-server:
  host: 127.0.0.1
  port: 28200
```

The existing standalone HTTP command remains available:

```bash
bub acp --transport http
```

The default endpoint is `http://127.0.0.1:28200/acp`; use `--host` and `--port` to override it. The default transport remains `stdio`. HTTP listens on loopback by default and does not add authentication; use it with trusted clients.

For negotiated HTTP/2, configure TLS or terminate HTTP/2 at a reverse proxy:

```bash
bub acp --transport http --host 127.0.0.1 --port 8443 \
  --certfile /path/to/cert.pem --keyfile /path/to/key.pem
```

Hypercorn supports HTTP/2; the TLS endpoint is `https://127.0.0.1:8443/acp`. Plain HTTP also works with clients that use HTTP/1.1 for local development. SDK HTTP clients negotiate HTTP/2 over TLS; setting `http2=True` alone does not enable cleartext HTTP/2 negotiation.

Connect using the SDK (inside an async function, with an ACP `Client` implementation):

```python
from acp import connect_to_agent
from acp.http import create_http_stream
from acp.schema import TextContentBlock

transport = create_http_stream("http://127.0.0.1:28200/acp")
connection = connect_to_agent(my_client, transport)
try:
    await connection.initialize(protocol_version=1)
    session = await connection.new_session(cwd="/path/to/workspace")
    await connection.prompt(
        session_id=session.session_id,
        prompt=[TextContentBlock(type="text", text="Hello")],
    )
finally:
    await connection.close()
```

Client-backed tools are selected from the capabilities advertised during initialization:

- `fs.readTextFile` enables client-backed `fs.read`.
- `fs.writeTextFile` enables client-backed `fs.write`.
- Both filesystem capabilities are required for client-backed `fs.edit`.
- `terminal` enables client-backed `bash`, `bash.output`, and `bash.kill` together.

Missing or false capabilities leave the corresponding Bub local tools in place. The ACP `update_plan` tool is always available.

### Asking the user

Clients that advertise `clientCapabilities.elicitation.form: {}` also enable the
session's `ask_user` tool. The tool sends `elicitation/create` with the current ACP
session ID and waits for the client's form response. For example:

```json
{
  "message": "Which approach should I use?",
  "requested_schema": {
    "type": "object",
    "properties": {
      "approach": {"type": "string", "enum": ["minimal", "full"]}
    },
    "required": ["approach"]
  }
}
```

Use ACP's form JSON Schema for `requested_schema`; string fields without an enum
allow free text. The tool's parameter schema exposes the SDK's `ElicitationSchema`
and its referenced field definitions so the model can see the supported form structure.
The tool returns JSON such as
`{"action":"accept","content":{"approach":"minimal"}}`,
`{"action":"decline"}`, or `{"action":"cancel"}`. Decline and cancel do not
supply an answer. Cancelling the active turn interrupts the pending request.

An omitted/null `elicitation.form`, an empty `elicitation` object, or URL-only
support does not enable this tool. In those cases the agent can ask in its normal
reply and wait for the user's next message. Clients must implement form elicitation
to display the UI; tool-execution permission dialogs are a separate mechanism.

### Connection isolation

Each connection has its own client capabilities and stream router. ACP prompts are serialized across connections and share session metadata so one connection cannot overwrite another's newly created sessions. In gateway mode, the gateway router remains bound; the ACP channel routes each turn's output to its client. Client-backed tools and plan instructions are scoped to ACP turns, and concurrent non-ACP turns continue using the original tools.

The dependency is pinned to **agent-client-protocol 1.0.0rc1**. HTTP uses `BubACPAgent` directly and supports initialization, new sessions, session loading and listing, config options, prompts, tool callbacks, and steering. Session stream registration and history replay are handled natively by the SDK, without a local compatibility adapter. Clients should also use SDK 1.0.0rc1 or an equivalent implementation of load request/response correlation, including sessions with empty history.

After initializing a new connection, load an existing session with `await connection.load_session(cwd="/path/to/workspace", session_id="existing-session-id")`, then continue with `connection.prompt(...)`. Keep the same Bub home and tape store when restarting the server to retain metadata and history.

HTTP does not advertise session resume/close because the SDK web adapter still does not enable those unstable routes. Stdio enables them by default. `connection.close()` terminates the HTTP connection with `DELETE`; automatic SSE reconnection is not provided by the SDK.

### WebSocket

Start the same web server using the explicit WebSocket option:

```bash
bub acp --transport websocket
```

The default WebSocket endpoint is `ws://127.0.0.1:28200/acp`. Use the same `--host`, `--port`, `--certfile`, and `--keyfile` options as HTTP; with TLS, connect using `wss://`. Both `--transport http` and `--transport websocket` start the SDK's combined HTTP/WebSocket endpoint, not protocol-exclusive listeners. The gateway's `acp-server` channel also accepts WebSocket connections without additional configuration. The same `bub-acp-server[http]` extra supplies all required dependencies.

In the SDK example above, replace transport creation with:

```python
from acp.ws import create_websocket_stream

transport = await create_websocket_stream("ws://127.0.0.1:28200/acp")
```

Initialization, session loading, prompts and tool callbacks work over the socket; `connection.close()` closes it. WebSocket does not require HTTP/2. Like HTTP, it does not advertise the SDK's disabled unstable resume/close routes and adds no authentication.

## Steering

Clients can detect steering support in the initialize response:

```json
{
  "_meta": {
    "steering": {
      "supported": true
    }
  }
}
```

Clients that require acknowledged steering can also negotiate the Lody
extension under `agentCapabilities._meta`:

```json
{
  "agentCapabilities": {
    "_meta": {
      "lody": {
        "steering": {
          "version": 1,
          "transport": "request",
          "upstreamTurn": "same",
          "configPolicy": "active"
        }
      }
    }
  }
}
```

Send a private ACP extension request while a turn is running or after it has become idle:

```json
{
  "method": "_lody/session/steer",
  "params": {
    "sessionId": "session-id",
    "prompt": [
      {
        "type": "text",
        "text": "Stop the current approach and inspect the failing test first."
      }
    ],
    "steerId": "client-generated-steer-id"
  }
}
```

The response outcome is `injected` when Bub consumes the message at the next model-step boundary, `startedNewTurn` when the previous turn has already passed its final boundary, or `failed` for an unexpected internal failure. Steering requests are serialized per session and preserve arrival order. The extension is private rather than part of the standard ACP method set, so clients must opt into it explicitly.

When the message is applied, Bub sends `_lody/session/steer_applied` with the
same `sessionId` and `steerId`, and returns `injected`. This lets the client
distinguish submission from application.

For compatibility with clients using the original Codex steering extension,
Bub also accepts `_session/steering`. Its optional `steerId` uses the matching
`_session/steering_applied` notification.

## Use In Zed

Zed supports external terminal agents through ACP. Custom agents are configured in Zed's `settings.json` under `agent_servers`.

Prerequisites:

- `bub` is installed and available to Zed.
- `bub-acp-server` is installed in the Bub environment:

```bash
bub install bub-acp-server@main
```

Open Zed's settings with the `zed: open settings` command and add a custom agent server:

```json
{
  "agent_servers": {
    "Bub": {
      "type": "custom",
      "command": "bub",
      "args": ["acp"],
      "env": {}
    }
  }
}
```

If Zed cannot find `bub`, use the absolute path printed by `command -v bub`:

```json
{
  "agent_servers": {
    "Bub": {
      "type": "custom",
      "command": "/absolute/path/to/bub",
      "args": ["acp"],
      "env": {}
    }
  }
}
```

After saving the settings, open Zed's agent panel with `cmd-?` on macOS or `ctrl-?` on Linux/Windows, then start a new thread and select `Bub`.

Useful Zed commands while testing:

- `dev: open acp logs` shows the JSON-RPC traffic between Zed and Bub.
- `zed: open settings` opens `settings.json`.

Notes:

- Zed launches Bub as a separate ACP process. Bub reads its own local configuration and credentials directly.
- Use `env` only for settings your Bub installation actually needs.
- If your Bub configuration is loaded from a project `.env`, use a wrapper command that loads that file before running `bub acp`.

References:

- Zed external agents documentation: https://zed.dev/docs/ai/external-agents
- Zed ACP client page: https://zed.dev/acp/editor/zed
