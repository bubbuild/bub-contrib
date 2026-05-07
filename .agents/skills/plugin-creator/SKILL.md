---
name: plugin-creator
description: |
  Create or update a Bub plugin in any local path. Use when the task is to scaffold a Python
  package that exposes a [project.entry-points.bub] entry, implement Bub hooks or tools, and make
  the plugin take effect by installing it or adding it as a dependency in the Bub runtime project.
  When working inside bub-contrib, also follow its package and workspace conventions.
---

# Bub Plugin Creator

Create Bub plugins as normal Python packages.

Core rule: a Bub plugin becomes effective only when both conditions are true:

1. the package exposes an entry point in the `bub` group
2. the package is installed in the same Python environment as Bub

Do not assume the plugin must live in `bub-contrib`. That repository is only one possible host.

## What Counts As A Bub Plugin

A Bub plugin is usually a Python package with:

- `pyproject.toml`
- `README.md`
- `src/<python_package>/__init__.py`
- `src/<python_package>/plugin.py` or another hook-exporting module such as `tools.py`
- optional helper modules such as `channel.py`, `store.py`, `jobstore.py`, `config.py`
- optional tests under `tests/`
- optional bundled agent skill files under `src/skills/<skill-name>/`

The package must export a Bub entry point through:

```toml
[project.entry-points.bub]
<plugin-name> = "<python_package>.plugin"
```

Other valid targets also exist, for example:

```toml
[project.entry-points.bub]
<plugin-name> = "<python_package>.tools"
<plugin-name> = "<python_package>.plugin:main"
```

Use the narrowest export surface that matches the implementation.

## First: Identify The Host Project

Before creating files, determine where the plugin should live and where Bub runs.

There are three common cases:

1. Existing monorepo package
   Create a new package inside the current repository and wire it into that repo's dependency flow.

2. Standalone local package
   Create a new package in any local path, then install it into the Bub environment with an editable
   or path-based dependency.

3. Existing package to extend
   Update the package in place and make sure the environment uses the updated dependency.

Important distinction:

- plugin source location: where files are created
- activation location: the project or environment that installs the package

If the task is ambiguous, infer both from nearby files such as `pyproject.toml`, `uv.lock`,
`.venv`, and how Bub is launched.

## Classify The Plugin Shape

Inspect the closest existing plugin before writing code. If working in `bub-contrib`, use packages
under `packages/` as the primary examples.

Common shapes:

1. Channel provider
   Use when the plugin connects Bub to an external message source or sink.
   Typical hook: `provide_channels`

2. Hook-only provider
   Use when the plugin contributes one focused hook such as model execution.
   Typical hook: `run_model`

3. Resource provider
   Use when the plugin returns a store or singleton runtime resource.
   Typical hook: `provide_tape_store`

4. Composite plugin
   Use when the plugin owns runtime state and also provides channels or tools.
   Typical hooks: `load_state` plus one or more provider hooks

5. Tool registration package
   Use when the package mainly exposes `@tool` functions and exports the tool module directly.

Prefer copying the nearest existing shape over inventing a new abstraction.

## Implementation Workflow

### 1. Read The Closest Reference

Open the closest existing package first.

Minimum files to inspect:

- host project's `pyproject.toml`
- closest plugin package `pyproject.toml`
- closest plugin entry-point module such as `plugin.py` or `tools.py`
- one or two representative tests
- `README.md`

If the plugin interacts with an agent-facing chat platform, also inspect any packaged skill such as
`src/skills/<channel>/SKILL.md`.

### 2. Choose The Package Location

Default structure for a new package:

```text
<plugin-root>/
├── pyproject.toml
├── README.md
├── src/
│   └── bub_<feature>/
│       ├── __init__.py
│       ├── plugin.py or tools.py
│       └── ...
└── tests/
    └── test_plugin.py
```

Naming conventions:

- distribution name: `bub-<feature>`
- Python package: `bub_<feature>`
- Bub entry point name: prefer the user-facing short name, usually `<feature>`

If working inside `bub-contrib`, the preferred location is:

```text
packages/bub-<feature>/
```

If working outside `bub-contrib`, create the package in the user-requested path or in the nearest
plugin-oriented subdirectory of the host project.

### 3. Implement Packaging Metadata

`pyproject.toml` should usually include:

- `name`
- `version`
- `description`
- `readme`
- `requires-python`
- runtime `dependencies`
- `[project.entry-points.bub]`
- build backend

Prefer `src/` layout unless the host project clearly uses another convention.

For `uv`-managed projects, path activation commonly looks like one of these:

```toml
[project]
dependencies = ["bub-my-plugin"]

[tool.uv.sources]
bub-my-plugin = { path = "../bub-my-plugin", editable = true }
```

or, in a workspace-style repo:

```toml
[tool.uv.workspace]
members = ["packages/*", "plugins/*"]

[tool.uv.sources]
bub-my-plugin = { workspace = true }
```

For ad hoc local activation without editing host metadata, an editable install is often enough:

```bash
uv pip install -e /abs/path/to/bub-my-plugin
```

Choose the activation method that matches the host project:

- persistent project dependency: update host `pyproject.toml`
- local development only: editable install may be sufficient
- monorepo workspace: add the package to workspace and source mapping if required

### 4. Implement The Bub Entry Module

Prefer the narrowest hook surface that solves the task.

Common patterns:

- `@hookimpl def provide_channels(...) -> list[Channel]`
- `@hookimpl async def run_model(...) -> str`
- `@hookimpl def provide_tape_store() -> ...`
- `@hookimpl def load_state(...) -> State`
- `@tool(name="...")` in a module exported directly as the Bub entry point

Guidelines:

- Keep the exported entry module thin when possible.
- Move protocol or platform code into helper modules such as `channel.py`, `store.py`, or `tools.py`.
- Use `pydantic-settings` or the host project's config approach when environment variables exist.
- Register every plugin configuration object with `@bub.config(name="<entry-point-name>")`.
  A configuration object means any `bub.Settings` subclass owned by the plugin, regardless of
  whether it lives in `plugin.py`, `config.py`, `channel.py`, `tools.py`, or another helper module.
- In plugin runtime code, read registered settings through `bub.ensure_config(ConfigClass)` instead
  of direct construction such as `ConfigClass()` or module-level settings singletons. Direct
  construction is only appropriate for tests, explicit factory injection, or standalone scripts that
  intentionally read environment variables without a Bub framework context.
- Use the Bub entry point name as the config section name, such as `qq`, `mcp`,
  `github-copilot`, or `tapestore-sqlite`. Keep environment variable prefixes specific, such as
  `BUB_QQ_` or `BUB_TAPESTORE_SQLITE_`.
- Cache singleton resources only when reuse is intentional and testable.
- Avoid framework-wide abstractions unless at least two packages actually need them.

### 5. Add Optional Agent Skill Files Only When Needed

Create `src/skills/<name>/SKILL.md` only if the plugin exposes agent-facing operational behavior,
for example a chat channel that needs explicit send, edit, or reply instructions.

Do not add a packaged agent skill for internal providers unless there is a real agent workflow to
teach.

If you add packaged skill files:

- keep them specific to the platform or workflow
- make command paths relative to the skill directory
- include scripts under `src/skills/<name>/scripts/`
- make sure packaging includes `SKILL.md` and scripts

### 6. Wire The Plugin Into The Bub Environment

This step is mandatory. Creating the package alone does not activate it.

Pick one of these activation paths:

1. Add as a normal dependency in the Bub host project
   Update host `pyproject.toml` dependencies and any source mapping such as `tool.uv.sources`.

2. Add as a workspace package
   Update workspace membership and source mapping so the host environment resolves the plugin.

3. Install directly into the active environment
   Use an editable or normal install such as `uv pip install -e /abs/path/to/plugin`.

When the task says "make it effective", prefer option 1 or 2 over a one-off install, unless the
user clearly wants a local experiment.

If the host project is `bub-contrib`, also check the root `pyproject.toml`. That file keeps
explicit root dependencies and `tool.uv.sources` entries for workspace packages.

### 7. Write The Minimum Useful README

Keep the README short and operational. Usually include:

- what the plugin provides
- required environment variables or configuration
- how to install or enable it
- any notable behavior or limitations

Do not pad it with generic packaging tutorials.

### 8. Add Targeted Tests

Non-trivial plugin behavior should have tests.

Favor narrow tests over large integration scaffolding.

Typical coverage:

- entry hook returns the right type or object
- settings parse environment variables correctly
- plugin-level singleton or factory behavior
- fallback and error-path behavior for boundary conditions

Use the host project's test style. In `bub-contrib`, that usually means:

- `pytest`
- direct imports from `<package>.plugin`
- `monkeypatch` for environment variables and runtime substitution
- `tmp_path` for filesystem behavior

## Decision Rules

- Prefer repository consistency over abstract elegance.
- Prefer one package per plugin, even if the implementation is small.
- Prefer `import bub` plus `@bub.config(name="<entry-point-name>")` for config registration so the
  decorator stays visually tied to the framework API.
- Prefer `bub.ensure_config(ConfigClass)` at the point of runtime use instead of module-level
  settings objects, so Bub-loaded config data and caching semantics are respected.
- Prefer explicit configuration names with a `BUB_<FEATURE>_` prefix when introducing new env vars.
- Prefer the minimum public surface area required by Bub hooks.
- Prefer persistent dependency wiring over ephemeral shell-only setup when the user asks to enable
  the plugin.

## Validation Checklist

Before finishing, verify:

1. Package name, Python module name, and Bub entry point are aligned.
2. The exported entry-point module only references modules that actually exist.
3. Dependencies in the plugin `pyproject.toml` match imported third-party packages.
4. The activation path is complete:
   either the host project depends on the package, or the package was installed into the runtime
   environment.
5. Every plugin-owned `bub.Settings` subclass has `@bub.config(name="<entry-point-name>")`, and
   importing the Bub entry-point target registers that config.
6. Runtime code uses `bub.ensure_config(ConfigClass)` for registered settings instead of direct
   `ConfigClass()` construction or module-level settings singletons.
7. Tests cover the main hook or configuration path.
8. README describes the behavior and enablement path that the implementation actually provides.
9. If packaged skills were added, the build config includes `SKILL.md` and scripts.

Recommended commands to suggest, adjusted to the host project:

```bash
uv run pytest <plugin-root>/tests
uv sync
```

For standalone local packages, also consider:

```bash
uv pip install -e /abs/path/to/plugin
```

## `bub-contrib` Notes

When the host project is this repository:

- create new plugins under `packages/bub-<feature>`
- use existing packages under `packages/` as primary examples
- update the root `pyproject.toml` if the new package should participate in the root dev environment
- if a packaged agent skill is needed, mirror the `src/skills/<name>/` convention used by channel
  plugins in this repo

## Output Contract

When using this skill to implement a plugin, the final response should state:

- where the plugin package was created or updated
- which Bub hooks or tools were implemented
- how the plugin was wired into the Bub environment
- whether a packaged agent skill was added
- what tests should be run
- any remaining assumptions, especially credentials, endpoints, and runtime environment
