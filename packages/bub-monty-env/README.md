# bub-monty-env

Runs Bub's code-mode `run_code` in [Monty](https://github.com/pydantic/monty), a sandboxed Python interpreter written in Rust, instead of a Python subprocess. Its `provide_environment` hook wraps whatever environment the other plugins provide (Docker, local sandbox, or Bub's host environment), so only `run_code` changes. A mixin and a wrapper function are also available for your own environments.

Monty suspends the code at every call that leaves the sandbox and hands it to Bub, which runs it and resumes the code. So:

- `tools.<name>(...)` calls still go through Bub's tool executor and hooks, as with the default runner; `asyncio.gather` runs them concurrently.
- File access (`pathlib.Path.read_text` / `write_text`) goes through the environment's `read_text` / `write_text`, so the code sees the same files as `fs.*`.
- The `republic` module is available, as with the default runner: its models call back into Bub, which runs them with its own credentials and settings. See [The `republic` module](#the-republic-module).
- The code cannot start processes, open sockets, read the host's environment variables or import third-party packages.
- Starting a run takes microseconds instead of a Python process start.

Requires Bub 0.5 or later. Only `run_code` changes: `bash` and `fs.*` keep running wherever the underlying environment runs them.

Chinese documentation: [README.zh-CN.md](./README.zh-CN.md)

## Python subset

Monty runs a subset of Python. Code that model-written glue usually needs works: functions, closures, comprehensions, f-strings, `try`/`except`, simple classes and `@dataclass`, `async`/`await` with `asyncio.gather`, and much of `json`, `re`, `datetime`, `collections`, `itertools`, `math`, `pathlib`. It rejects class inheritance, `match`, generators (`yield`), `async with`, `async for`, `del`, method decorators (`@property`) and every third-party package. When code fails on unsupported syntax, the error tells the model it runs in Monty so it can rewrite the code. See Monty's [limitations](https://pydantic.dev/docs/monty/limitations/).

Use it when code-mode code is glue between tools; keep the default runner when the model needs `pandas`, `numpy` or other packages.

## The `republic` module

Bub's default runner gives code-mode code a `republic` module for media and model calls; this plugin provides the same module in Monty. Its model calls reach Bub as `republic.<operation>` calls on `run_code`'s `call_tool`, with Republic values as `{"$republic": ...}` JSON objects, the same protocol as the default runner, so Bub serves them unchanged and the code never sees provider credentials.

```python
from republic import Image, get_model, user

model = get_model()  # the model running this session; or get_model("provider:model")
chart = Image("image/png", url="https://example.com/chart.png")
reply = await model.chat([user("Describe this chart.", chart)])
print(reply.text, reply.token_usage.total_tokens)
```

Available: `Image` / `Audio` / `Video`, `Message`, `system` / `user` / `assistant`, `get_model(spec=None).chat(...)`, `get_embedding_model(spec).embed(...)` / `embed_many(...)`, `get_decision_model(spec).decide(...)` with `republic.decisions.Noul` / `Choice` / `Score`. Media in `tools.*` arguments and results crosses as Republic values too.

Differences from the default runner, which come from Monty:

- `republic` is a preset global. Monty cannot import modules it does not ship, so before running the code the plugin rewrites `import republic` and `from republic[.decisions] import ...` into assignments from that global, keeping line numbers.
- No streaming: `model.stream(...)` needs `async with` and `async for`, which Monty does not support. Use `await model.chat(...)` for the whole response, and use models without `async with`.

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
| `sessions` | `["*"]` | Globs over session ids (`<channel>:<chat id>`); others keep the environment other plugins provide, unwrapped |
| `filesystem` | `read-write` | File access from the code: `read-write`, `read-only` or `none` |
| `max_memory_mb` | `256` | Heap limit per run; `null` for none |
| `max_recursion_depth` | `1000` | Maximum call stack depth |
| `max_suspensions` | `10000` | Maximum tool calls, file accesses and other host round trips per run |
| `max_processes` | CPU count | Monty worker processes per session |

The run time limit is `run_code`'s own `timeout_seconds`.

## Combining with other environments

The plugin's hook runs first (`tryfirst`), asks the other plugins for the session's environment (pluggy's `subset_hook_caller`), and wraps it with `create_monty_environment`. With `bub-docker-env` or `bub-sandbox-env` installed, `bash` and `fs.*` keep running in Docker or the sandbox, and `run_code` runs in Monty with file access through that environment. When no other plugin provides one, it wraps Bub's host environment. Sessions outside `sessions` get the other plugins' environment unchanged.

For your own environments:

```python
from bub_monty_env import MontyCodeMixin, MontyOptions, create_monty_environment

# Wrap an instance: everything but run_code is delegated to it, and closing the
# wrapper closes it.
environment = create_monty_environment(MyEnvironment(...), MontyOptions(filesystem="read-only"))


# Or mix into a class: the mixin goes first.
class MyMontyEnvironment(MontyCodeMixin, MyEnvironment):
    monty_options = MontyOptions(filesystem="read-only")
```

`MontyEnvironment` is Bub's host environment with Monty `run_code`.

Lower-level pieces: `run_code_in_monty(pool, code, ...)` implements `Environment.run_code` on a `pydantic_monty.AsyncMonty` pool, and `MontyCodeMixin.monty_os_handler()` can be overridden to change what file access the code gets.

## Limits

- `open()` is not supported; the code reads and writes files with `pathlib.Path`. `Path.exists`, `iterdir`, `mkdir` and other file operations are not available.
- Monty is young; behavior may differ from CPython in places the limitations page lists.
- Tool results cross into the sandbox as JSON-compatible data, as with the default runner.
- `Path.read_bytes` is not available, so media from files needs `data=` from elsewhere, or `tools.attach_image(image="<path>")` to let Bub load it.
