# bub-dynamic-workflows

Bee-on-tape dynamic workflow coordination for `bub`.

## What It Provides

- Bub plugin entry point: `dynamic-workflows`
- A lifecycle channel named `workflow`
- Bub tools:
  - `workflow.start`
  - `workflow.status`
  - `workflow.resume`
  - `workflow.cancel`
- CLI commands under `bub workflow`
- A packaged `workflow` skill that teaches agents when and how to use workflow tools

The plugin treats workflows as long-lived bee tasks over tape. Tools and CLI commands submit lifecycle commands to a `WorkflowController`; the controller owns lifecycle transitions, cancellation, resume, and runtime execution. Redun evaluates the workflow DAG, including per-item `foreach` fan-out, while Bub subagents perform node work and tape records task, node, and checkpoint boundaries.

## Minimal Workflow

```yaml
name: repo_review
description: Review a repository from multiple perspectives.
args_schema:
  type: object
  properties:
    repo:
      type: string
  required:
    - repo
nodes:
  - id: inventory
    prompt: |
      Inspect the repository at {args.repo}.
  - id: tests
    depends_on: [inventory]
    prompt: |
      Review tests using this inventory:
      {nodes.inventory}
  - id: summary
    depends_on: [inventory, tests]
    prompt: |
      Produce a concise final report.
      Inventory: {nodes.inventory}
      Tests: {nodes.tests}
```

## Commands

```bash
bub workflow start workflow.yaml --args args.json --run-id review-1
bub workflow status review-1
bub workflow resume review-1
bub workflow cancel review-1
```

Planning is workflow behavior, not a lifecycle command. A workflow that needs decomposition should express it as ordinary nodes and templates, then start through the same validated runtime path. Agents should use the packaged `workflow` skill for that operating policy.

## Tape Contract

Workflow runs write anchors such as:

- `workflow/<run_id>/task_init`
- `workflow/<run_id>/node/<node_id>/init`
- `workflow/<run_id>/node/<node_id>/finish`
- `workflow/<run_id>/dag_checkpoint/<seq>`
- `workflow/<run_id>/task_finish`
- `workflow/<run_id>/task_error`
- `workflow/<run_id>/task_cancelled`

They also write `workflow.*` events. `.bub/workflows/<run_id>/task.json` is a readable status projection for local status/resume convenience; Redun's `redun.db` and tape remain the execution evidence trail.
