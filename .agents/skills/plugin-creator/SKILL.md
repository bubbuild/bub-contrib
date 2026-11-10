---
name: plugin-creator
description: |
  Create or update a Bub plugin at any local path. Use when the task is to scaffold a Python
  package that exposes a [project.entry-points.bub] entry, implement Bub hooks or tools, or make
  a plugin take effect by installing it into, or adding it as a dependency of, the project that
  runs Bub. Inside bub-contrib, also follow the repository's package and workspace conventions.
---

# Bub Plugin Creator

A Bub plugin is an ordinary Python package. It takes effect only when both of these hold:

1. The package declares an entry point in the `bub` group.
2. The package is installed in the same Python environment as Bub.

The plugin does not have to live in `bub-contrib`; that repository is just one possible host.

## Anatomy Of A Bub Plugin

A typical plugin package contains:

- `pyproject.toml`
- `README.md`
- `src/<python_package>/__init__.py`
- `src/<python_package>/plugin.py`, or another module that exports hooks or tools, such as `tools.py`
- optional helper modules such as `channel.py`, `store.py`, `jobstore.py`, or `config.py`
- optional tests under `tests/`
- optional bundled agent skills under `src/skills/<skill-name>/`

The entry point is declared like this:

```toml
[project.entry-points.bub]
<plugin-name> = "<python_package>.plugin"
```

The target can also be a tools module or a specific object:

```toml
[project.entry-points.bub]
<plugin-name> = "<python_package>.tools"
<plugin-name> = "<python_package>.plugin:main"
```

Point the entry point at the smallest target that covers the implementation.

## Step 0: Identify The Host Project

Before creating any files, work out two separate things:

- **Source location**: where the plugin's files live.
- **Activation location**: the project or environment that installs the package and runs Bub.

Most tasks fall into one of three cases:

1. **New package in an existing monorepo.** Add the package to the current repository and wire it
   into that repository's dependency setup.
2. **Standalone local package.** Create the package at the requested path, then install it into
   the Bub environment as an editable or path dependency.
3. **Existing package.** Update it in place and make sure the environment picks up the change.

If the request does not say, infer both locations from nearby `pyproject.toml`, `uv.lock`, and
`.venv` files, and from how Bub is launched.

## Classify The Plugin Shape

Find the closest existing plugin and read it before writing code. In `bub-contrib`, the packages
under `packages/` are the primary references.

Common shapes:

| Shape | Use when the plugin... | Typical hooks |
| --- | --- | --- |
| Channel provider | connects Bub to an external message source or sink | `provide_channels` |
| Hook-only provider | contributes one focused hook, such as model execution | `run_model` |
| Resource provider | returns a store or another singleton runtime resource | `provide_tape_store` |
| Composite plugin | owns runtime state and also provides channels or tools | `load_state` plus provider hooks |
| Tool package | mainly defines `@tool` functions | entry point targets the tools module |

Copy the nearest existing shape instead of inventing a new abstraction.

## Implementation Workflow

### 1. Read The Closest Reference

At a minimum, read:

- the host project's `pyproject.toml`
- the closest plugin's `pyproject.toml`, entry module (`plugin.py` or `tools.py`), and `README.md`
- one or two of its representative tests

If the plugin integrates an agent-facing chat platform, also read any bundled skill such as
`src/skills/<channel>/SKILL.md`.

### 2. Choose The Package Layout

Default layout for a new package:

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
- entry point name: the short user-facing name, usually `<feature>`

In `bub-contrib`, put new packages at `packages/bub-<feature>/`. Elsewhere, use the path the user
asked for, or the host project's existing plugin directory if it has one.

### 3. Write The Packaging Metadata

`pyproject.toml` usually declares:

- `name`, `version`, `description`, `readme`, and `requires-python`
- runtime `dependencies`
- `[project.entry-points.bub]`
- a build backend

Use the `src/` layout unless the host project clearly follows another convention.

### 4. Implement The Entry Module

Implement only the hooks the task needs.

Common patterns:

- `@hookimpl def provide_channels(...) -> list[Channel]`
- `@hookimpl async def run_model(...) -> str`
- `@hookimpl def provide_tape_store() -> ...`
- `@hookimpl def load_state(...) -> State`
- `@tool(name="...")` in a module that the entry point targets directly

Guidelines:

- Keep the entry module thin. Put protocol and platform code in helper modules such as
  `channel.py`, `store.py`, or `tools.py`.
- Choose tool `exposure` deliberately. The default `"auto"` suits most tools; use `"direct"` for
  tools the model must always call directly, `"code"` for tools callable only from `run_code`,
  and `"command"` for operator comma commands the model must never see.
- If the plugin reads environment variables, use `pydantic-settings` or the host project's
  existing configuration approach.
- Register every configuration class the plugin owns with
  `@bub.config(name="<entry-point-name>")`. This covers every `bub.Settings` subclass, whichever
  module defines it.
- At runtime, read settings with `bub.ensure_config(ConfigClass)`. Do not instantiate
  `ConfigClass()` directly or keep module-level settings singletons. Direct construction is only
  for tests, explicit factory injection, or standalone scripts that run outside Bub and
  intentionally read environment variables.
- Name the config section after the entry point, such as `qq`, `mcp`, `github-copilot`, or
  `tapestore-sqlite`, and give environment variables a specific prefix such as `BUB_QQ_` or
  `BUB_TAPESTORE_SQLITE_`.
- Cache singleton resources only when reuse is intentional and covered by tests.
- Do not add shared abstractions until at least two packages actually need them.

### 5. Bundle An Agent Skill Only When Needed

Add `src/skills/<name>/SKILL.md` only when the plugin has agent-facing behavior to teach, such as
a chat channel that needs explicit send, edit, or reply instructions. Internal providers usually
do not need one.

If you bundle a skill:

- keep it specific to the platform or workflow
- write command paths relative to the skill directory
- put scripts under `src/skills/<name>/scripts/`
- make sure the build includes `SKILL.md` and the scripts

### 6. Activate The Plugin In The Bub Environment

This step is required: creating the package does not activate it. Choose one path:

1. **Dependency of the host project.** Add the package to the host's `dependencies`, plus a source
   mapping if needed:

   ```toml
   [project]
   dependencies = ["bub-my-plugin"]

   [tool.uv.sources]
   bub-my-plugin = { path = "../bub-my-plugin", editable = true }
   ```

2. **Workspace member.** Add the package to the workspace and map it as a workspace source:

   ```toml
   [tool.uv.workspace]
   members = ["packages/*", "plugins/*"]

   [tool.uv.sources]
   bub-my-plugin = { workspace = true }
   ```

3. **Direct install.** Install the package into the active environment without editing host
   metadata:

   ```bash
   uv pip install -e /abs/path/to/bub-my-plugin
   ```

When the user asks to make the plugin take effect, use option 1 or 2 so the setup persists. Use
option 3 only when the user clearly wants a local experiment.

In `bub-contrib`, also check the root `pyproject.toml`, which lists workspace packages explicitly
in its dependencies and `tool.uv.sources`.

### 7. Write A Short README

Keep the README short and practical. It usually covers:

- what the plugin provides
- required environment variables or configuration
- how to install and enable it
- notable behavior and limitations

Leave out generic packaging tutorials.

### 8. Add Targeted Tests

Test any non-trivial behavior, preferring narrow tests to large integration setups. Typical
coverage:

- the entry hook returns the expected type or object
- settings parse environment variables correctly
- singleton or factory behavior at the plugin level
- fallback and error paths at boundary conditions

Follow the host project's test style. In `bub-contrib`, that usually means `pytest`, direct
imports from `<package>.plugin`, `monkeypatch` for environment variables and runtime
substitution, and `tmp_path` for filesystem behavior.

## Decision Rules

- Match existing repository conventions, even when another design looks cleaner.
- Use one package per plugin, however small.
- Register config with `import bub` and `@bub.config(name="<entry-point-name>")`, so the decorator
  visibly belongs to the framework API.
- Read config with `bub.ensure_config(ConfigClass)` where it is used, so Bub-loaded config data and
  caching apply.
- Prefix new environment variables with `BUB_<FEATURE>_`.
- Expose no more public surface than the Bub hooks require.
- When the user asks to enable the plugin, wire it in as a persistent dependency rather than a
  one-off shell install.

## Validation Checklist

Before finishing, confirm that:

1. The distribution name, Python package name, and entry point name match each other.
2. The entry point target imports only modules that exist.
3. The plugin's `pyproject.toml` declares every third-party package it imports.
4. Activation is complete: the host project depends on the package, or the package is installed
   in the runtime environment.
5. Every plugin-owned `bub.Settings` subclass has `@bub.config(name="<entry-point-name>")`, and
   importing the entry point target registers it.
6. Runtime code reads registered settings through `bub.ensure_config(ConfigClass)`, not direct
   `ConfigClass()` construction or module-level settings singletons.
7. Tests cover the main hook or configuration path.
8. The README describes what the implementation actually does and how to enable it.
9. If skills are bundled, the build includes `SKILL.md` and its scripts.
10. If the user explicitly asked to publish from `bub-contrib`, the code is on the default branch
    and the publish request title names the exact package and version.

Suggested commands, adjusted to the host project:

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
- use existing packages under `packages/` as the primary references
- update the root `pyproject.toml` if the package should be part of the root dev environment
- for a bundled agent skill, follow the `src/skills/<name>/` convention used by the channel
  plugins

### Publish Requests

Creating or updating a plugin in `bub-contrib` does not imply publishing it. Unless the user
explicitly asks to publish or to open a publish request, do not open an issue and do not run
`uv publish` locally.

Before requesting a release, confirm that the code is merged into the default branch. Then open an
issue with the repository's **Publish request** issue template, titled exactly:

```text
Request for publish: <package> <version>
```

For example:

```text
Request for publish: bub-example 1.2.3
```

The publish request workflow checks that:

- the title contains a valid package name and a PEP 440 version
- the package exists under `packages/`, and its directory name matches `project.name`
- `uv version --dry-run` accepts the requested version
- that package version has not already been published to PyPI

The PyPI check queries `https://pypi.org/pypi/<package>/<version>/json`. Only HTTP 404 means the
version is available; an existing release, any other HTTP error, or a network error blocks the
release.

A successful validation does not post a comment. After review, a maintainer adds the
`to-be-published` label; the workflow validates again, then builds and publishes the package
through PyPI Trusted Publishing. GitHub Actions comments the release result on the issue: it
closes the issue on success and removes the label on failure so the request can be retried after
a fix.

Do not bypass the version check by editing the workflow, uploading manually, or rewriting the
version string into an equivalent form. If the user asks for a publish request but the code is not
yet on the default branch, explain that prerequisite instead of opening the request early.

## Output Contract

After implementing a plugin with this skill, the final response states:

- where the plugin package was created or updated
- which Bub hooks or tools it implements
- how it was wired into the Bub environment
- whether a bundled agent skill was added
- which tests to run
- whether a `bub-contrib` publish request was explicitly requested, opened, or left as a next step
- any remaining assumptions, especially about credentials, endpoints, and the runtime environment
