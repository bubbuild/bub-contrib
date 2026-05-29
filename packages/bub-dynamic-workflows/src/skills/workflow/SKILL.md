---
name: workflow
description:
  Use Bub workflow tools for multi-step, resumable, dependency-aware work that benefits
  from tape checkpoints, parallel node execution, or repeated per-item processing.
metadata:
  channel: workflow
---

# Workflow Skill

Use this skill when the task is more reliable as a Bub workflow than as a single long agent turn.

Prefer workflows for:

- multi-step analysis with clear dependencies
- repeated work over a list of targets
- long-running tasks that should support `status`, `resume`, and `cancel`
- work that needs tape-visible task, node, and checkpoint evidence
- final synthesis from several specialized reviews

Do not use a separate planning lifecycle command. Planning is normal workflow behavior: express decomposition, review gates, and synthesis as workflow nodes.

## Tool Policy

Use these tools:

- `workflow.start` to start a spec or template; it validates spec and args before creating a run.
- `workflow.status` to inspect a run.
- `workflow.resume` to continue an incomplete run.
- `workflow.cancel` only when the user asks to stop or when continuing would be harmful.

Do not call `workflow.start` repeatedly for the same intended run. Use `workflow.status` first, then `workflow.resume` when a run already exists.

## Spec Shape

Keep specs small and explicit. Each node should have one clear responsibility.

Use `depends_on` for true data or ordering dependencies. Leave independent nodes without dependencies so Redun can execute them concurrently.

Use `foreach` when one node should run once per item from `args` or a prior node output.
Each `foreach` item is a Redun-visible task; use it for real fan-out/fan-in work, not just prompt formatting.
Set node-level `concurrency` when a fan-out node needs tighter throttling than the workflow default.

Use `output_schema` when later nodes depend on structured data.

Example:

```yaml
name: repo_review
description: Review a repository with parallel specialized checks.
args_schema:
  type: object
  properties:
    repo:
      type: string
  required: [repo]
nodes:
  - id: inventory
    prompt: |
      Inspect {args.repo} and return the main modules, entry points, and tests.
  - id: api_review
    depends_on: [inventory]
    prompt: |
      Review API boundaries using this inventory:
      {nodes.inventory}
  - id: test_review
    depends_on: [inventory]
    prompt: |
      Review test coverage using this inventory:
      {nodes.inventory}
  - id: summary
    depends_on: [api_review, test_review]
    prompt: |
      Summarize findings.
      API review: {nodes.api_review}
      Test review: {nodes.test_review}
```

## Operating Rules

- Start from an existing template when one matches the task.
- Keep prompts specific enough that subagents can finish without extra context.
- Put shared input in `args` instead of duplicating it across prompts.
- Name nodes with stable lowercase identifiers.
- Make the final node synthesize outputs instead of asking every node for a final answer.
- If a node fails, inspect `workflow.status` before resuming.
- Treat tape and Redun's `redun.db` as the workflow evidence trail.
- Treat `.bub/workflows/<run_id>/task.json` as a readable status projection, not as a separate source of workflow truth.
