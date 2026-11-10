# bub-docker-env

Runs Bub's `bash`, `bash.output`, `bash.kill`, `fs.*` and code-mode `run_code` tools inside Docker containers instead of on the host, through Bub's `provide_environment` hook. Use it when people you do not fully trust can make the agent run commands, for example through a chat channel.

Requires Bub 0.5 or later. Older Bub would ignore the hook and keep running commands on the host, so the package refuses to install there.

Chinese documentation: [README.zh-CN.md](./README.zh-CN.md)

## What runs where

- **Which sessions**: every session by default. Session ids look like `<channel>:<chat id>`, so `sessions: "qq:*"` keeps only QQ chats in Docker while your own `bub chat` / `bub run` stay on the host.
- **What moves into the container**: processes (`bash`, `run_code`) and file access (`fs.*`). Tool semantics stay in Bub: timeouts, background shells, output rendering and hooks are unchanged. Other tools (the channel's own tools, `web.fetch`, MCP tools, other plugins' tools) still run in Bub's process on the host.
- **What the container sees**: the Bub workspace, mounted **at its own path** (Bub's prompt tells the model that path, so it must exist in the container too), and skill directories outside the workspace (`~/.agents/skills` and skills shipped by installed plugins), mounted **read-only at their host paths** so scripts a skill tells the model to run are found. Nothing else from the host. Keep secrets (`~/.bub/config.yml`, API keys, state files) outside the workspace.
- **What it can change**: only the workspace and `/tmp`. The workspace is writable by design, so a command someone approves can still delete everything in it: keep nothing irreplaceable there.

Every container starts with:

| Setting | Default |
| --- | --- |
| network | none at all |
| user | the host user's uid:gid, so files in the workspace keep their owner |
| privileges | all capabilities dropped, `no-new-privileges` |
| limits | 1 GB memory, 1 CPU, 256 processes |
| root filesystem | read-only; `/tmp` is a 512 MB tmpfs; `HOME` is `/tmp`, so anything written under `~` is gone when the container is removed |
| image | `python:3.12-slim` |

## Lifecycle

- A session's container starts on its first tool call that needs it (`bash`, `fs.*` or `run_code`). Starting takes a second or two; the image is pulled on first use.
- Before a container is used, the plugin compares the workspace's entries with what the container sees at the workspace path, and refuses (and removes) the container when none match; see [Deployment notes](#deployment-notes).
- Containers are removed when Bub stops. With `scope: shared`, the one container is removed when the last session using it closes, which also happens when Bub stops.
- If Docker is unavailable, the tool call fails. Commands never fall back to the host.
- Each process starts its own session inside the container and records its process group, so timeouts and `bash.kill` stop the whole tree, as on the host.

Containers are named `bub-env-<12 hex chars>` and labelled `bub.docker-env=1` and `bub.docker-env.key=<session id, or "shared">`.

## Configuration

In the `docker_env:` section of `~/.bub/config.yml`, or as environment variables `BUB_DOCKER_ENV_<KEY>` (e.g. `BUB_DOCKER_ENV_NETWORK=bridge`). Bub reads them at startup, so restart Bub after a change.

```yaml
docker_env:
  sessions: "qq:*"
  network: none
```

| Key | Default | Values | What it does |
| --- | --- | --- | --- |
| `sessions` | `"*"` | comma-separated globs over session ids, e.g. `"qq:*"`, `"qq:*,feishu:*"`; empty matches nothing | Sessions that run in Docker. Others keep Bub's host environment |
| `scope` | `session` | `session`, `shared` | `session`: one container per session. `shared`: one container for all matching sessions; cheaper, but sessions see each other's processes and `/tmp` |
| `image` | `python:3.12-slim` | any image reference | Must provide `bash`, `sh`, `setsid` (util-linux), `cat`, `mkdir`, `dirname` and `ls`; `python3` is needed only for `run_code` |
| `network` | `none` | `none`, `bridge`, or the name of a Docker network | `bridge` lets commands reach the internet **and your local network** |
| `memory` | `1g` | Docker memory size, e.g. `512m`, `2g` | `docker run --memory` |
| `cpus` | `"1"` | number of CPUs, e.g. `"0.5"`, `"2"` | `docker run --cpus` |
| `pids_limit` | `256` | integer, at least 16 | `docker run --pids-limit` |
| `user` | `""` | `""`, `"uid:gid"`, `none` | `""`: the host user's uid:gid. `none`: pass no `--user` (rootless Docker, Podman) |
| `workdir` | `""` | `""` or an absolute path | Where the workspace appears in the container. `""`: the workspace's own path (recommended) |
| `read_only` | `true` | `true`, `false` | Read-only root filesystem with a tmpfs `/tmp`. `false` makes the root filesystem writable, which alone rarely allows installing software; see [Adding tools](#adding-tools) |
| `tmp_size` | `512m` | Docker tmpfs size, e.g. `256m`, `1g` | Size of `/tmp` when `read_only` is on |
| `mount_skills` | `true` | `true`, `false` | Mount skill directories outside the workspace read-only at their host paths |
| `host_workspace` | `""` | `""` or an absolute path on the Docker host | The workspace as the Docker daemon sees it, when Bub and the daemon do not share a filesystem. Skill directories are not mounted then |
| `mount_options` | `""` | bind mount options, e.g. `z`, `Z` | Applied to every bind mount; switches to the `--volume` syntax, so mounted paths must not contain `:` |
| `extra_run_args` | `""` | extra `docker run` arguments, split like a shell command line | e.g. `--runtime=runsc` (gVisor), `--userns=keep-id` (Podman) |
| `docker` | `docker` | a Docker-compatible CLI on Bub's `PATH`, or its full path | The CLI the plugin calls |

### Adding tools

To give commands tools the default image lacks (`git`, `jq`, Python packages), build an image with them rather than installing at runtime:

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git jq \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir requests
```

```bash
docker build -t my-bub-sandbox:1 .
```

```yaml
docker_env:
  image: my-bub-sandbox:1
```

The container stays read-only, offline and unprivileged, and the tools are there in every new container. Installing at runtime instead needs more than `read_only: false`: commands run as a non-root user with all capabilities dropped, so system directories stay unwritable and package managers fail; the default `network: none` leaves no package source; and whatever gets installed is lost when the container is removed. Loosen `read_only`, `user` and `network` together only if you accept the weaker isolation.

## Deployment notes

The Docker daemon resolves bind mounts **on its own host**, not where Bub runs. When the two differ, the workspace path can silently mount an empty or unrelated directory; the mount check refuses such a container, and the error points at `host_workspace`. An empty workspace cannot be checked; the log then shows `docker_env.mount.unverified`.

| Deployment | What to set | Tested |
| --- | --- | --- |
| Bub on the Docker host | nothing | macOS with Docker Desktop |
| Bub in a container using the host's `docker.sock`, or a remote `DOCKER_HOST` | `host_workspace`: the workspace path on the Docker host | refusal of a wrong mount only |
| SELinux (Fedora, RHEL) | `mount_options: z` | no |
| Rootless Docker | `user: none` | no |
| Podman | `docker: podman`, `user: none`, `extra_run_args: "--userns=keep-id"` | no |
| gVisor | `extra_run_args: "--runtime=runsc"` | no |
| Docker Desktop | the workspace must be under a shared path (Settings → Resources → File sharing) | yes |

Linux and macOS hosts are supported; Windows is not. Kubernetes clusters and hosted agent sandboxes usually expose no Docker daemon; they need an environment provider for their own API.

## Logs

| Event | Level | Meaning |
| --- | --- | --- |
| `docker_env.session` | INFO | A session got a Docker environment (with its container name and scope) |
| `docker_env.session.host` | DEBUG | A session did not match `sessions` and stays on the host |
| `docker_env.container.started` | INFO | A container started (image, network) |
| `docker_env.container.removed` | INFO | A container was removed |
| `docker_env.mount.unverified` | INFO | The workspace was empty, so the mount check could not compare entries |
| `docker_env.signal_failed` | WARNING | Stopping a process in the container could not be requested |

A refused mount or a container that fails to start surfaces as the tool call's error.

## Install

`bub-docker-env` is not on PyPI. With a global Bub install:

```bash
bub install bub-docker-env@main
```

For local development, install the package path into the environment that runs `bub`:

```bash
uv tool install bub --with /path/to/bub-contrib/packages/bub-docker-env
```

Docker (Docker Desktop, OrbStack or Docker Engine) must be running, and the `docker` CLI must be on the `PATH` of the Bub process.

## Cleanup

After a crash or `kill -9`, containers keep running. Remove them with:

```bash
docker rm -f $(docker ps -q --filter label=bub.docker-env=1)
```

The plugin does not do this at startup, since other Bub instances on the same Docker host may be using their containers.
