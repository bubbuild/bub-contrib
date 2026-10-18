# bub-monty-env

Runs Bub's code-mode `run_code` in [Monty](https://github.com/pydantic/monty), a sandboxed Python interpreter written in Rust, instead of a Python subprocess. It works through Bub's `provide_environment` hook, and also gives you a mixin and a wrapper to put Monty `run_code` on top of any other environment (Docker, local sandbox, ...).

Monty suspends the code at every call that leaves the sandbox and hands it to Bub, which runs it and resumes the code. So:

- `tools.<name>(...)` calls still go through Bub's tool executor and hooks, as with the default runner; `asyncio.gather` runs them concurrently.
- File access (`pathlib.Path.read_text` / `write_text`) goes through the environment's `read_text` / `write_text`, so the code sees the same files as `fs.*`.
- The code cannot start processes, open sockets, read the host's environment variables or import third-party packages.
- Starting a run takes microseconds instead of a Python process start.

Requires Bub 0.5 or later. Only `run_code` changes: `bash` and `fs.*` keep running wherever the underlying environment runs them.

Chinese documentation: [README.zh-CN.md](./README.zh-CN.md)

## Python subset

Monty runs a subset of Python. Code that model-written glue usually needs works: functions, closures, comprehensions, f-strings, `try`/`except`, simple classes and `@dataclass`, `async`/`await` with `asyncio.gather`, and much of `json`, `re`, `datetime`, `collections`, `itertools`, `math`, `pathlib`. It rejects class inheritance, `match`, generators (`yield`), `del`, method decorators (`@property`) and every third-party package. When code fails on unsupported syntax, the error tells the model it runs in Monty so it can rewrite the code. See Monty's [limitations](https://pydantic.dev/docs/monty/limitations/).

Use it when code-mode code is glue between tools; keep the default runner when the model needs `pandas`, `numpy` or other packages.

## Configuration

In the `monty_env:` section of `~/.bub/config.yml`, or as environment variables `BUB_MONTY_ENV_<KEY>` (lists are JSON: `BUB_MONTY_ENV_SESSIONS='["qq:*"]'`). Restart Bub after a change.

```yaml
monty_env:
  sessions: ["qq:*"]
  filesystem: read-only
  max_memory_mb: 256
```

| Key | Default | What it does |
| --- | --- | --- |
| `sessions` | `["*"]` | Globs over session ids (`<channel>:<chat id>`); others keep Bub's host environment |
| `filesystem` | `read-write` | File access from the code: `read-write`, `read-only` or `none` |
| `max_memory_mb` | `256` | Heap limit per run; `null` for none |
| `max_recursion_depth` | `1000` | Maximum call stack depth |
| `max_suspensions` | `10000` | Maximum tool calls, file accesses and other host round trips per run |
| `max_processes` | CPU count | Monty worker processes per session |

The run time limit is `run_code`'s own `timeout_seconds`.

## Combining with other environments

The plugin provides `MontyEnvironment`: Bub's host environment with Monty `run_code`. Bub uses the first environment a plugin provides, so to run `bash` in Docker or a local sandbox and `run_code` in Monty, put Monty on top of that environment in your own plugin:

```python
import bub
from bub import hookimpl
from bub_monty_env import MontyCodeMixin, MontyOptions, create_monty_environment
from bub_sandbox_env.config import SandboxEnvConfig
from bub_sandbox_env.environment import SandboxEnvironment


# Wrap an instance: everything but run_code is delegated to it, and closing the
# wrapper closes it.
@hookimpl
def provide_environment(session_id, workspace):
    config = bub.ensure_config(SandboxEnvConfig)
    return create_monty_environment(SandboxEnvironment(config, workspace=workspace))


# Or mix into a class: the mixin goes first.
class SandboxMontyEnvironment(MontyCodeMixin, SandboxEnvironment):
    monty_options = MontyOptions(filesystem="read-only")
```

Lower-level pieces: `run_code_in_monty(pool, code, ...)` implements `Environment.run_code` on a `pydantic_monty.AsyncMonty` pool, and `MontyCodeMixin.monty_os_handler()` can be overridden to change what file access the code gets.

## Limits

- `open()` is not supported; the code reads and writes files with `pathlib.Path`. `Path.exists`, `iterdir`, `mkdir` and other file operations are not available.
- Monty is young; behavior may differ from CPython in places the limitations page lists.
- Tool results cross into the sandbox as JSON-compatible data, as with the default runner.
