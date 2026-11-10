# bub-sandbox-env

Runs Bub's `bash`, `bash.output`, `bash.kill`, `fs.*` and code-mode `run_code` tools in a lightweight local OS sandbox, through Bub's `provide_environment` hook: **Seatbelt** (`sandbox-exec`) on macOS, **bubblewrap** (`bwrap`) on Linux. Windows is not supported.

It is the lightweight sibling of [`bub-docker-env`](../bub-docker-env/README.md): no daemon, no image, no startup delay, and commands use the tools already installed on your machine. In exchange the isolation is weaker than a container's: processes run as you, on your host, and see your filesystem read-only.

Requires Bub 0.5 or later.

Chinese documentation: [README.zh-CN.md](./README.zh-CN.md)

## What the sandbox allows

| | Default |
| --- | --- |
| Read | the whole filesystem, **except** `deny_read`: `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`, `~/.config/gcloud`, `~/.kube`, `~/.docker`, `~/.netrc`, `~/.git-credentials`, `~/.npmrc`, `~/.pypirc` and Bub's home (`$BUB_HOME`, `~/.bub`: config with API keys) |
| Write | the workspace, a per-session temp directory (`$TMPDIR`; `/tmp` on Linux), paths in `writable`; on macOS also the per-user temp and cache directories (`getconf DARWIN_USER_TEMP_DIR`), which `git`, `xcrun` and others need |
| Network | none (`network: true` to allow) |
| Environment | Bub's environment minus variables matching `env_deny`: `BUB_*`, `*_API_KEY`, `*_TOKEN`, `*_SECRET`, `*_SECRET_KEY`, `*_PASSWORD`, `AWS_*` |

When paths overlap, the more specific one wins: a workspace inside `~/.bub` stays usable, and adding `<workspace>/.env` to `deny_read` keeps that `.env` unreadable and unwritable.

What runs where:

- **Which sessions**: every session by default. Session ids look like `<channel>:<chat id>`, so `sessions: ["qq:*"]` sandboxes only QQ chats.
- **What is sandboxed**: processes (`bash`, `run_code`) and file access (`fs.*`, which goes through the same sandbox, so the tools and the shell see one policy). Tool semantics stay in Bub. Other tools (channel tools, `web.fetch`, MCP tools, other plugins' tools) still run in Bub's process, unsandboxed.
- **Stopping**: each process starts its own session, as with Bub's host environment, so timeouts and `bash.kill` stop the whole tree.
- **Fails closed**: if the backend cannot run (no `sandbox-exec`, no `bwrap`), the tool call fails. Commands never fall back to running unsandboxed.

## Configuration

In the `sandbox_env:` section of `~/.bub/config.yml`, or as environment variables `BUB_SANDBOX_ENV_<KEY>` (e.g. `BUB_SANDBOX_ENV_NETWORK=true`; lists are JSON: `BUB_SANDBOX_ENV_WRITABLE='["~/.cache/uv"]'`). Restart Bub after a change.

```yaml
sandbox_env:
  sessions: ["qq:*"]
  network: false
  writable:
    - ~/.cache/uv
    - ~/.npm
```

| Key | Default | What it does |
| --- | --- | --- |
| `sessions` | `["*"]` | Globs over session ids; others keep Bub's host environment. An empty list matches nothing |
| `backend` | `auto` | `seatbelt`, `bwrap`, or `auto` (Seatbelt on macOS, bubblewrap elsewhere) |
| `network` | `false` | Allow network access |
| `writable` | `[]` | Extra writable paths, e.g. package caches (`~/.cache/uv`, `~/.npm`) |
| `deny_read` | see above | Unreadable paths; `~` and `$BUB_HOME` expand. Setting it replaces the default list |
| `env_deny` | see above | Case-insensitive globs of environment variable names to drop |
| `bwrap` | `bwrap` | The bubblewrap executable |

## Platform notes

**macOS.** `sandbox-exec` ships with macOS. It is marked deprecated but Codex and Claude Code use it for the same purpose. The profile denies by default and allows only the services ordinary command-line tools need, so `open` and AppleScript cannot reach other apps.

**Linux.** Install bubblewrap (`apt install bubblewrap`, `dnf install bubblewrap`). Every process gets a read-only view of `/`, a private `/tmp` backed by the session temp directory, its own PID and IPC namespaces, and no network namespace access unless `network` is on. Unprivileged bubblewrap needs user namespaces: on Ubuntu 23.10+ AppArmor may block them, see `kernel.apparmor_restrict_unprivileged_userns`. Inside Docker, the container usually needs `--security-opt seccomp=unconfined --security-opt apparmor=unconfined` (or `--privileged`).

## Limits

- This is defense against mistakes and casual misuse, not a hard boundary against a determined attacker. For untrusted users prefer `bub-docker-env`, ideally with gVisor.
- Everything outside `deny_read` is readable. Keep other secrets out of readable places, or add them to `deny_read`.
- The workspace is writable by design; a command can still delete everything in it.
- There are no CPU, memory or process limits.
- On macOS the per-user temp directory is shared with your other programs and with other sandboxed sessions.
