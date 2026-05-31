---
name: workflow
description: Use when a Bub agent should run a tape-backed bee workflow inspired by tapexbee: topic-scoped work, template-first decomposition, DAG anchors, structured node outputs, checkpoints, and durable evidence recorded through Bub tape.
---

# Workflow

Use this skill for a tapexbee-style bee topic: one bounded task with a clear brief, explicit nodes, human-reviewable milestones, tape evidence, and a final status projection.

For ordinary linear work, one small edit, or a direct command, use the normal available tools. Use workflow when decomposition improves evidence, reviewability, context control, or retry boundaries.

## Tapexbee Principles

- Treat the workflow as a bee topic with a brief, a template, node turns, checkpoints, and a terminal projection.
- Keep milestones human-reviewable and evidence-oriented.
- Use DAG anchors as the memory spine: task start, node start, node finish, checkpoint, task finish, and task error.
- Make every node small enough that its prompt and output stay inspectable.
- Let later nodes consume structured outputs from earlier nodes instead of relying on hidden parent-agent reasoning.
- Keep dynamic expression in the template. Create a temporary template for one-off work, or use a reusable template when the same bee shape should be run repeatedly.

## When To Use

Use workflow for:

- Repository audits, architecture reviews, multi-perspective code reviews, or research tasks.
- Work that benefits from independent inspection nodes followed by verification or synthesis.
- Tasks that should leave durable tape evidence for future review, debugging, or memory retrieval.
- User requests that mention workflow, bee, tapexbee, fan-out, multi-agent review, checkpointed execution, or DAG-style decomposition.

Use normal tools for:

- A single small edit or one direct command.
- A question answerable from the current context.
- Work that requires nodes to share mutable in-memory state.
- Deployment-grade scheduling, rollback, or isolation concerns.

## Template Model

Call `workflow.start` with one `template` object. Include `nodes` for an inline temporary template. Omit `nodes` to load a reusable template by `name`.

Template sources:

- Use `template.inputs` for run-specific values.
- Define allowed inputs in `input_schema`.
- A reusable template may come from the host state registry or from a workspace bundle at `.bub/workflow/templates/<name>/workflow.yaml`.
- Runtime paths are configured through Bub settings with the `BUB_WORKFLOW_` environment prefix. `projection_dir` controls task projections, and `template_dirs` controls reusable template lookup.

Template request fields:

- `name`: inline template name or reusable template name.
- `inputs`: values for this run.

Inline template definition fields:

- `description`: reviewable intent and scope.
- `skill`: optional task-specific operating guidance.
- `config`: optional metadata for the host.
- `input_schema`: JSON Schema properties for `template.inputs`.
- `nodes`: DAG nodes ordered by dependency.

Node fields:

- `id`: unique stable node id.
- `title`: human-readable milestone name.
- `description`: scope and acceptance criteria.
- `executor`: `subagent` for Bub subagent work, or `function` for deterministic Python work.
- `prompt`: template text for the node. Required for `subagent`, optional for `function`.
- `call`: Python callable target for `function` executor.
- `depends_on`: node ids that must complete first.
- `output_schema`: JSON schema for outputs consumed by later nodes.
- `allowed_tools` and `allowed_skills`: bounds for Bub subagent execution.
- `features`: compact feature notes that explain why the node exists.

Prompt references:

- `{brief}` reads the task brief.
- `{inputs.name}` reads a template input.
- `{nodes.node_id}` reads a prior node output.
- `{nodes.node_id.field}` reads a field from a prior structured node output.

Reference completed dependency outputs.

## Execution

- `workflow.start` creates the projection, writes tape start evidence, and executes ready nodes when `execute` is true.
- `workflow.step` resumes an existing bee task and executes currently ready nodes. Use this after manual review or when `execute` was false.
- `workflow.status` returns the current task projection.

Use `subagent` nodes for analysis, implementation review, and synthesis. Use `function` nodes for deterministic adapters and repeatable tests.

## Minimal Template Definition

Use the same template fields for a temporary template object or for a reusable template bundle. Store reusable templates as `workflow.yaml`.

```yaml
name: repo_review
description: Review a repository through bee milestones.
skill: Inspect evidence first, then verify findings before synthesis.
input_schema:
  focus:
    type: string
    default: maintainability
nodes:
  - id: inventory
    title: Inventory
    executor: subagent
    prompt: Inspect this repository for {inputs.focus}. Return key modules and risks.
    output_schema:
      type: object
      properties:
        modules:
          type: array
          items:
            type: string
        risks:
          type: array
          items:
            type: string
      required:
        - modules
        - risks
  - id: verify
    title: Verify
    executor: subagent
    depends_on:
      - inventory
    prompt: |-
      Verify these modules and risks:
      {nodes.inventory}
    output_schema:
      type: object
      properties:
        verdict:
          type: string
        next_steps:
          type: array
          items:
            type: string
      required:
        - verdict
        - next_steps
```

## Tape Evidence

The workflow writes anchors and events through the configured Bub tape store:

- `bee/<run_id>/bee_task_init`
- `bee/<run_id>/bee_node/<node_id>/init`
- `bee/<run_id>/bee_node/<node_id>/finish`
- `bee/<run_id>/bee_dag_checkpoint`
- `bee/<run_id>/bee_task_fin`
- `bee/<run_id>/bee_task_error`

For quick status, call `workflow.status`. For retrospective analysis, query the Bub tape store around the bee anchors.
