# bub-schedule

Scheduling plugin for the `bub` ecosystem.

## What It Provides

- Bub plugin entry point: `schedule`
- A scheduler channel backed by `APScheduler`
- A JSON-based job store persisted under Bub runtime home
- Built-in tools:
  - `schedule.add`
  - `schedule.remove`
  - `schedule.list`

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-schedule"
```

You can also install it with Bub:

```bash
bub install bub-schedule@main
```

## Runtime Behavior

- Scheduler starts when the plugin channel starts.
- Jobs are persisted to:
  - `<runtime_home>/jobs.json`
- Scheduled reminders are sent back into Bub as channel messages.

## Provided Tools

- `schedule.add`: Add a scheduled job with a cron expression and a message.
- `schedule.remove`: Remove a scheduled job by ID.
- `schedule.list`: List all scheduled jobs.
