# bub-cursor

Cursor CLI-backed model plugin for `bub`.

## What It Provides

- Bub plugin entry point: `cursor`
- A `run_model` hook implementation that invokes the Cursor CLI
- `bub login cursor`, which delegates to Cursor CLI login
- Session continuation via `<cursor-cli> --resume=<session_id>`
- JSON output parsing from `<cursor-cli> -p ... --output-format json`
- Optional temporary skill wiring from installed Bub `skills` into workspace `.agents/skills`

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-cursor"
```

You can also install it with Bub:

```bash
bub install bub-cursor@main
```

## Prerequisites

- Cursor CLI must be installed and available in `PATH`.
- Cursor CLI must have authentication available through either saved browser login
  or `CURSOR_API_KEY`.

Depending on how Cursor CLI was installed, the executable may be named
`cursor-agent` or `agent`. Homebrew installs `cursor-agent`; the curl installer
usually installs `agent`.

Verify the CLI with whichever command exists:

```bash
cursor-agent --version
# or
agent --version
```

Browser login is available through Cursor CLI:

```bash
cursor-agent login
# or
agent login
```

or through Bub:

```bash
bub login cursor
```

CLI path resolution uses this order: `BUB_CURSOR_CLI_PATH`, `cursor-agent`,
then `agent`.

## Configuration

The plugin reads environment variables with prefix `BUB_CURSOR_`:

- `BUB_CURSOR_MODEL`: optional model name passed as `--model <value>`.
- `BUB_CURSOR_CLI_PATH`: Cursor CLI executable path. When unset, the plugin
  tries `cursor-agent`, then `agent`.
- `BUB_CURSOR_TIMEOUT_SECONDS`: subprocess timeout. Defaults to `300`.

Cursor CLI also reads its own authentication environment variables, such as
`CURSOR_API_KEY`, directly.
When neither saved Cursor login nor `CURSOR_API_KEY` is available, the plugin
raises `No Cursor authentication found. Run \`bub login cursor\` first or set \`CURSOR_API_KEY\`.`

## Runtime Behavior

- Workspace resolution:
  - Uses `state["_runtime_workspace"]` when present
  - Falls back to current working directory
- Command shape:
  - `<cursor-cli> -p <prompt> --output-format json`
  - `<cursor-cli> --resume=<session_id> -p <prompt> --output-format json`
- The plugin stores Cursor session IDs in `<workspace>/.bub-cursor-threads.json`.
- Cursor CLI stdout is parsed as JSON; the `result` field is returned as model output.

## Skill Integration

- During invocation, the plugin scans `skills` for directories containing `SKILL.md`.
- It creates symlinks under `<workspace>/.agents/skills/<skill_name>`.
- Symlinks created by this plugin invocation are removed after the run.

## Notes

Cursor CLI non-interactive mode can modify files in the selected workspace.
