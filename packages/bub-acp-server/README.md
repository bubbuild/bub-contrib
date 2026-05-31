# bub-acp-server

Expose Bub as an Agent Client Protocol agent.

## What It Provides

- Bub plugin entry point: `acp-server`
- CLI command registered on Bub: `bub acp serve`
- Standalone console script: `bub-acp-server`
- ACP agent methods for `initialize`, `session/new`, `session/load`, `session/resume`, `session/list`, `session/close`, and `session/prompt`
- Streaming ACP `session/update` events from Bub stream events

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
bub acp serve
```

or:

```bash
bub-acp-server
```

The process speaks ACP over stdio. Prompts are sent through Bub's hook pipeline with stream output enabled, so model chunks and tool events can be displayed by the ACP client as they arrive.

Bub keeps using its own configuration, tools, skills, and tapes. The ACP client starts the process and displays the session; it does not replace Bub's model setup.

ACP session metadata is stored under Bub home as `acp-sessions.json` so compatible clients can list sessions again after restarting. Keep `BUB_HOME` stable if you want the same ACP thread list across editor launches.

`bub-acp-server` supports both ACP session load and resume. `session/load` restores the matching Bub history through the same ACP streaming path used by live turns. `session/resume` attaches the editor back to the Bub session without replaying history, so later turns keep streaming through Bub's normal hook pipeline.

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
      "args": ["acp", "serve"],
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
      "args": ["acp", "serve"],
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
- If your Bub configuration is loaded from a project `.env`, use a wrapper command that loads that file before running `bub acp serve`.

References:

- Zed external agents documentation: https://zed.dev/docs/ai/external-agents
- Zed ACP client page: https://zed.dev/acp/editor/zed
