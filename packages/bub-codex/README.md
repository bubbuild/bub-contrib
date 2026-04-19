# bub-codex

Codex model plugin for `bub`.

## What It Provides

- Bub plugin entry point: `codex`
- A `run_model` hook implementation that invokes the `codex` CLI
- Session continuation via `codex e resume <session_id>`
- Optional temporary skill wiring from `skills` into workspace `.agents/skills`

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-codex"
```

You can also install it with Bub:

```bash
bub install bub-codex@main
```

## Prerequisites

- `codex` CLI must be installed and available in `PATH`.
- `codex` CLI should be authenticated before runtime.

## Configuration

The plugin reads environment variables with prefix `BUB_CODEX_`:

- `BUB_CODEX_MODEL` (optional): model override passed as `--model <value>`
- `BUB_CODEX_YOLO_MODE` (optional, default: `false`): when `true`, appends `--dangerously-bypass-approvals-and-sandbox`

## Runtime Behavior

- Workspace resolution:
  - Uses `state["_runtime_workspace"]` when present
  - Falls back to current working directory
- Command shape:
  - `codex e resume <session_id> [--model ...] [--dangerously-bypass-approvals-and-sandbox] -`
- Prompt is sent through stdin; stdout is returned as model output.
- When Codex exits non-zero, output includes: `Codex process exited with code <code>.`

## Skill Integration

- During invocation, the plugin scans `skills` for directories containing `SKILL.md`.
- It creates symlinks under `<workspace>/.agents/skills/<skill_name>`.
- Symlinks created by this plugin invocation are removed after the run.
