# bub-acp-server

Expose Bub as an Agent Client Protocol agent.

## What It Provides

- Bub plugin entry point: `acp-server`
- CLI command registered on Bub: `bub acp`
- Standalone console script: `bub-acp-server`
- ACP agent methods for `initialize`, `session/new`, `session/load`, `session/resume`, `session/list`, `session/close`, and `session/prompt`
- Streaming ACP `session/update` events from Bub stream events
- ACP client-backed replacements for Bub's `bash`, `fs.read`, `fs.write`, and `fs.edit` tools while the ACP server is running
- An ACP-aware `update_plan` tool that updates the client plan UI and records each complete plan as a `plan` event in the session tape
- Automatic recovery of the latest persisted plan into the next ACP turn's model context
- Session-scoped model and reasoning-effort selection through ACP config options
- ACP context-compaction notifications when `tape.handoff` runs
- Mid-turn steering through the `_session/steering` ACP extension

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-acp-server"
```

Or from a Bub project:

```bash
bub install bub-acp-server@main
```

## Usage

Configure an ACP-compatible client to launch one of:

```bash
bub acp
```

The previous `bub acp serve` form remains accepted temporarily and prints a deprecation warning. Other positional arguments are rejected.

or:

```bash
bub-acp-server
```

The process speaks ACP over stdio. Prompts are sent through Bub's hook pipeline with stream output enabled, so model chunks and tool events can be displayed by the ACP client as they arrive.

The agent sends an ACP `usage_update` whenever the streamed usage snapshot changes, with a final end-of-stream check as a fallback. Missing token usage is reported as `0`. If the model provider does not report its context-window size, set `BUB_ACP_SERVER_CONTEXT_WINDOW_SIZE`; the default is `128000` tokens.

ACP clients can select both the model and reasoning effort for each session. Reasoning effort defaults to `auto`; the selected value is persisted with the ACP session and passed into Bub's turn state for subsequent model calls.

While the ACP server is running, it replaces Bub's `tape.handoff` tool with an equivalent implementation and reports the operation as a context-compaction tool call. Compatible clients receive `Context compacting` and `Context compacted` updates marked with `_meta.contextCompaction`.

Bub keeps using its own configuration, tools, skills, and tapes. The ACP client starts the process and displays the session; it does not replace Bub's model setup.

ACP session IDs remain the protocol-facing `chat_id`. Bub namespaces its internal session ID with the ACP channel before selecting a tape, so an equal session ID from another channel cannot reuse the ACP tape.

ACP session metadata is stored under Bub home as `acp-sessions.json` so compatible clients can list sessions again after restarting. Keep `BUB_HOME` stable if you want the same ACP thread list across editor launches.

`bub-acp-server` supports both ACP session load and resume. `session/load` restores the matching Bub history through the same ACP streaming path used by live turns. `session/resume` attaches the editor back to the Bub session without replaying history, so later turns keep streaming through Bub's normal hook pipeline.

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

Send a private ACP extension request while a turn is running or after it has become idle:

```json
{
  "method": "_session/steering",
  "params": {
    "sessionId": "session-id",
    "prompt": [
      {
        "type": "text",
        "text": "Stop the current approach and inspect the failing test first."
      }
    ]
  }
}
```

The response outcome is `injected` when Bub consumes the message at the next model-step boundary, `startedNewTurn` when the previous turn has already passed its final boundary, or `failed` for an unexpected internal failure. Steering requests are serialized per session and preserve arrival order. The extension is private rather than part of the standard ACP method set, so clients must opt into it explicitly.

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
