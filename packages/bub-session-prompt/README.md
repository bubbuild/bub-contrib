# bub-session-prompt

Session-specific system prompt plugin for `bub`.

## What It Provides

- Bub plugin entry point: `session-prompt`
- A `system_prompt` hook implementation
- Per-session prompt content loaded from `~/.bub/sessions/<session_id>/AGENTS.md`
- Automatic creation of the session prompt directory when a session is active

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-session-prompt"
```

You can also install it with Bub:

```bash
bub install bub-session-prompt@main
```

## Runtime Behavior

- When `state["session_id"]` is present, the plugin reads:
  - `~/.bub/sessions/<session_id>/AGENTS.md`
- If the session directory does not exist yet, it is created automatically.
- If `AGENTS.md` does not exist, the current session prompt content is treated as empty.
- The plugin injects a `<session_instruct>` block into the system prompt that:
  - tells the agent where the session prompt file lives
  - includes the current `session_id`
  - embeds the current contents of `AGENTS.md`
- When `state["session_id"]` is missing, the injected block still renders, but the current prompt content is empty.

## Usage Notes

- Edit `~/.bub/sessions/<session_id>/AGENTS.md` to change the system prompt for a specific session.
- This package does not define additional environment variables or external service dependencies.
