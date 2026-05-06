---
name: handoff
description: |
  Trigger a phase transition when the current task stage is complete or context is getting too large.
  Use when: (1) you have finished a logical unit of work and want to hand off to a fresh session,
  (2) the current context is getting long and a clean break would help,
  (3) the task is moving into a fundamentally different stage.
---

# Handoff

Trigger a handoff to start a fresh session with continuation context via http-bridge.

## Usage

```bash
bash ${SKILL_DIR}/scripts/handoff.sh --name "phase-name" --summary "What was accomplished"
```

## Parameters

- `--name`: A short identifier for this phase transition (e.g., "discovery-complete", "implementation-done")
- `--summary`: Brief summary of what was accomplished in this phase

## How It Works

The script sends a `,tape.handoff` command to the current session via the http-bridge endpoint.
The session_id and bridge URL are available as environment variables (`BUB_SESSION_ID`, `BUB_BRIDGE_URL`).

## Important

After triggering handoff, stop working immediately. The next session will pick up from your summary.