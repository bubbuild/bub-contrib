# bub-dsh

DeepSeek Harness Python SDK-backed `run_model` plugin for `bub`.

## What It Provides

- Bub plugin entry point: `dsh`
- A `run_model` hook backed by the official `deepseek-harness-sdk`
- Runtime-local DeepSeek Harness session mapping for each Bub `session_id`

## Installation

Install using Bub's plugin manager:

```bash
bub install bub-dsh@main
```

Install directly from GitHub:

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-dsh"
```

## Authentication

The plugin uses Bub's root model configuration. For example:

```bash
export BUB_MODEL=dsh:deepseek-v4-flash
export BUB_MAX_TOKENS=49152
export BUB_DSH_API_KEY=sk-your-key
export BUB_DSH_API_BASE=https://api.deepseek.com
```

`BUB_API_KEY` and `BUB_API_BASE` are also supported by Bub for non-provider-specific
values.

The root provider `dsh` maps to the DeepSeek Harness provider route
`deepseek-official`. An unqualified root model defaults to `dsh`; any other explicit
provider is passed to DeepSeek Harness unchanged.

## Configuration

Environment variables use the `BUB_DSH_` prefix:

- `BUB_DSH_REQUEST_TIMEOUT_SECONDS`: optional positive JSON-RPC request timeout
- `BUB_DSH_SHUTDOWN_TIMEOUT_SECONDS`: runtime shutdown timeout, default `1`
- `BUB_DSH_CORDIS`: optional custom Cordis configuration path
- `BUB_DSH_SESSION_ROOT`: session storage directory, default `<bub.home>/dsh`

## Runtime Behavior

The DeepSeek Harness SDK is synchronous, so the plugin runs it in a worker thread and
does not block Bub's async event loop. Runtime processes are reused until Bub exits.
Calls for the same Bub session reuse one runtime-local session id, preserving the
Harness conversation and persistent shell state without colliding with logs left by a
previous Bub process. Session storage defaults to `bub.home / "dsh"`.

String prompts and structured lists of JSON content blocks are passed to
`DeepSeekHarness.run()` unchanged.

When a run finishes with `finish_reason="error"`, the plugin raises
`DshRunError` instead of returning an empty response. The Harness error payload is
available unchanged through the exception's `error` attribute.

The bundled runtime includes local shell tools and can modify files visible from the
Bub workspace. Run it only with filesystem permissions appropriate for the task.

## Current Limitations

- DeepSeek Harness is in developer preview and may introduce breaking SDK changes.
- Restarting Bub creates new Harness session ids. The current SDK cannot restore a
  persisted session into a new runtime process, although existing logs remain on disk.
- Cancelling the Bub coroutine cannot forcibly stop work already running in Python's
  worker thread. The current SDK has no whole-turn timeout while it waits for the
  runtime to become idle.

## Validation

```bash
uv run pytest packages/bub-dsh/tests
uv run ruff check packages/bub-dsh
uv sync
```
