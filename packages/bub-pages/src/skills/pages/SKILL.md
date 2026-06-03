---
name: pages
description: Use bub-pages to register, publish, and locally serve language-agnostic static site artifacts. Use when a user or Agent needs to deploy one or more static pages, dashboards, reports, demos, or lightweight browser apps from existing build output.
metadata:
  plugin: bub-pages
---

# Bub Pages Skill

Use this skill when the task is to deploy or preview static pages through `bub-pages`.

## Deployment Policy

- Treat the static artifact directory as the deployment unit.
- Do not require the user's site source code to live inside the Bub workspace, this plugin, or this repository.
- Do not move source code into `bub-contrib` just to publish it.
- If the site uses a framework or generator, build it wherever it already lives and register the resulting output directory.
- For public internet exposure, prefer a production static host or put the Uvicorn ASGI server behind a process manager, reverse proxy, and CDN where appropriate.

## Workflow

1. Identify the artifact directory that contains browser-loadable files such as `index.html`, CSS, JavaScript, images, or generated assets.
2. If the artifact does not exist yet, identify the build command and its working directory.
3. Register the site:

```bash
bub pages add SITE_NAME /absolute/or/relative/artifact-dir --path /SITE_NAME
```

4. If a build step is needed, register it without changing the artifact boundary:

```bash
bub pages add SITE_NAME /path/to/artifact-dir \
  --build-dir /path/to/project \
  --build "BUILD COMMAND" \
  --path /SITE_NAME
```

5. Publish:

```bash
bub pages publish SITE_NAME
```

6. Preview or share on a trusted network:

```bash
bub pages serve --host 127.0.0.1 --port 8000
```

## Multi-Site Rules

- Use one site name per artifact directory.
- Use distinct URL paths for each site, for example `/docs`, `/dashboard`, and `/demo`.
- Run `bub pages list` before adding a new site to avoid path collisions.
- Use `bub pages remove SITE_NAME --purge` when both registry entry and published files should be removed.

## Checks Before Publishing

- The artifact directory exists after any build command runs.
- The artifact directory has a top-level `index.html` when the site should load at its mount path.
- The artifact directory does not contain symbolic links.
- The artifact does not contain secrets, private data, or development-only files.
- Links and asset paths work under the configured `--path`.
