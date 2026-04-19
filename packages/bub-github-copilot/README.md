# bub-github-copilot

GitHub Copilot SDK-backed `run_model` plugin for `bub`.

## What It Provides

- Bub plugin entry point: `github-copilot`
- A `run_model` hook implementation that delegates model execution to the GitHub Copilot SDK
- `bub login github` and `bub login github-copilot` commands for GitHub device-flow login
- Token fallback from persisted OAuth tokens, `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, or `gh auth token`

## Why This Package Uses The SDK

This package intentionally uses `github-copilot-sdk` instead of wiring `republic` into Bub.

That keeps the plugin close to `bub-codex`:

- Bub delegates model execution to an external coding agent backend
- GitHub Copilot keeps its own session state and tool runtime
- the plugin stays small and focused on auth, prompt adaptation, and session wiring

## Installation

Install from the monorepo package directory during local development:

```bash
uv add --editable /path/to/bub-contrib/packages/bub-github-copilot
```

Install directly from GitHub:

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-github-copilot"
```

You can also install it with Bub:

```bash
bub install bub-github-copilot@main
```

## Login

Authenticate once with GitHub device flow:

```bash
bub login github
```

The plugin stores the resulting token at:

- `~/.bub/github_copilot_auth.json`

## Configuration

Environment variables use the `BUB_GITHUB_COPILOT_` prefix:

- `BUB_GITHUB_COPILOT_MODEL`: optional Copilot model override, for example `gpt-5`
- `BUB_GITHUB_COPILOT_REASONING_EFFORT`: optional reasoning level, one of `low`, `medium`, `high`, `xhigh`
- `BUB_GITHUB_COPILOT_TIMEOUT_SECONDS`: request timeout for one `run_model` call, default `300`
- `BUB_GITHUB_COPILOT_LOG_LEVEL`: Copilot CLI log level, default `error`
- `BUB_GITHUB_COPILOT_CLI_PATH`: optional explicit Copilot CLI binary path

## Runtime Behavior

- Internal Bub commands such as prompts starting with `,` are still delegated to the runtime agent
- Normal prompts are sent to a Copilot SDK session derived from the Bub `session_id`
- The plugin sets `working_directory` to Bub's runtime workspace
- If the prompt contains Bub multimodal image parts, data URLs are converted into Copilot blob attachments
- Workspace skills under `.agents/skills` are passed to Copilot when present

## Current Limitations

- Only Bub data URL image parts are adapted to SDK attachments
- Remote image URLs are rejected
- The plugin relies on the SDK's bundled Copilot CLI, which is still marked technical preview upstream

## Validation

Recommended local checks:

```bash
uv run pytest packages/bub-github-copilot/tests
uv sync
```
