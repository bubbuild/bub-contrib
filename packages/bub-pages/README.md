# bub-pages

Language-agnostic static pages plugin for `bub`.

## What It Provides

- Bub plugin entry point: `pages`
- Multi-site static page registry, similar to GitHub Pages projects
- CLI commands under `bub pages`:
  - `list`
  - `show`
  - `add`
  - `remove`
  - `publish`
  - `serve`
- Pure Python publishing and serving with no Node, Go, Rust, or framework-specific runtime dependency

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-pages"
```

You can also install it with Bub:

```bash
bub install bub-pages@main
```

## Usage

Register a static site directory:

```bash
bub pages add docs ./website/dist --path /docs
```

Register a build artifact that can be refreshed before publishing:

```bash
bub pages add app ./frontend/dist --build-dir ./frontend --build "npm run build" --path /app
```

Publish one site, publish all sites, then serve them:

```bash
bub pages publish docs
bub pages publish
bub pages serve --host 127.0.0.1 --port 8000
```

## Examples

Two minimal examples are included:

- [`examples/static-artifact`](./examples/static-artifact): publish an existing static artifact directory.
- [`examples/generated-artifact`](./examples/generated-artifact): run a language-agnostic build command that writes an artifact directory, then publish that artifact.

## Runtime Behavior

- Configuration defaults to `~/.bub/pages.json`.
- Published files default to `~/.bub/pages/sites/<site-name>`.
- `artifact` is the static output directory to publish. It can be outside the Bub workspace.
- `build` is optional and is executed before publishing.
- `build_dir` is optional and controls the working directory for `build`.
- Artifacts containing symbolic links are rejected during publish to avoid serving files outside the artifact tree.
- Sites are mounted by URL path, so multiple sites can be served from one process:
  - `/docs`
  - `/app`
  - `/`
- The built-in server uses Python's `http.server` and is intended for local sharing, previews, and simple trusted deployments. Put it behind production-grade hosting when public internet exposure matters.

## Design Notes

`bub-pages` follows the same static-artifact boundary used by static hosting systems: publish files that browsers can load directly. GitHub Pages documents static files and custom build artifacts as the deployment unit, while OpenAI's June 2026 Codex Sites preview emphasizes shareable interactive pages and lightweight tools for workspaces. This plugin keeps that idea local and language-agnostic: any generator can produce the artifact directory, and `bub-pages` only registers, copies, and serves the result.
