# bub-kimi

Kimi CLI plugin for `bub`.

## What It Provides

- Bub plugin entry point: `kimi`
- A `run_model` hook implementation that invokes the `kimi` CLI
- Session continuation via `kimi -r <session_id>`
- Optional temporary skill wiring from `skills` into workspace `.agents/skills`

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-kimi"
```

## Prerequisites

- `kimi` CLI must be installed and available in `PATH`.
- Authenticate with either:
  - `kimi login`, or
  - Env vars: `BUB_KIMI_BASE_URL`, `BUB_KIMI_MODEL_NAME`, `BUB_KIMI_API_KEY`.

## Configuration

The plugin reads environment variables with prefix `BUB_KIMI_`:

- `BUB_KIMI_MODEL_NAME` (optional): model name forwarded to the `kimi` CLI as the `KIMI_MODEL_NAME` environment variable.
- `BUB_KIMI_API_KEY` (optional): API key forwarded to the `kimi` CLI as the `KIMI_API_KEY` environment variable. Useful when you want to drive the CLI without interactive `kimi login`.
- `BUB_KIMI_BASE_URL` (optional): base URL forwarded to the `kimi` CLI as the `KIMI_BASE_URL` environment variable. Use it to point the CLI at a custom/self-hosted Kimi endpoint.

## Runtime Behavior

- Workspace resolution:
  - Uses `state["_runtime_workspace"]` when present
  - Falls back to current working directory
- Command shape:
  - `kimi [-r <session_id>] --quiet -p <prompt>`
- Stdout is returned as model output (`--quiet` prints the final assistant message only).
- Session ID is parsed from stderr line `To resume this session: kimi -r <uuid>` and persisted in `.bub-kimi-threads.json`.

## Skill Integration

- During invocation, the plugin scans `skills` for directories containing `SKILL.md`.
- It creates symlinks under `<workspace>/.agents/skills/<skill_name>`.
- Symlinks created by this plugin invocation are removed after the run.
