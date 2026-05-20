# bub-extism

Extism WebAssembly bridge plugin for `bub`.

## What It Provides

- Bub plugin entry point: `extism`
- One Bub hook adapter per configured Extism plug-in
- Standard Extism manifest support
- `bub extism` management commands:
  - `list`
  - `show`
  - `add`
  - `remove`
- Python-side proxies for hook surfaces that need Bub runtime objects:
  - `run_model_stream`
  - `provide_channels`
  - `provide_tape_store`
  - `register_cli_commands`

`bub-extism` does not replace Bub's pluggy model. It loads as a normal Bub
plugin, then registers one hook adapter for each configured wasm plug-in.

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-extism"
```

You can also install it with Bub:

```bash
bub install bub-extism@main
```

## Prerequisites

- Python 3.12+
- The `extism` Python runtime package is installed with this package
- WASI-enabled modules must set `"wasi": true` in Bub config

For example builds:

- Rust example:
  - `cargo`
  - `rustup`
  - `wasm32-unknown-unknown` target
- Go example:
  - Go with `GOOS=wasip1 GOARCH=wasm` support

## Configuration

By default, `bub-extism` reads `~/.bub/extism.json`.

Use `BUB_EXTISM_CONFIG_PATH=/path/to/extism.json` to override the config path.

Example:

```json
{
  "plugins": {
    "prompt": {
      "manifest": {
        "wasm": [
          {
            "path": "/absolute/path/to/prompt.wasm"
          }
        ]
      },
      "hooks": {
        "build_prompt": "build_prompt"
      }
    },
    "model": {
      "manifest": {
        "wasm": [
          {
            "path": "/absolute/path/to/model.wasm"
          }
        ],
        "allowed_hosts": ["api.example.com"],
        "config": {
          "provider": "demo"
        }
      },
      "wasi": true,
      "hooks": {
        "run_model": "run_model"
      }
    }
  }
}
```

Configuration rules:

- Each entry under `plugins` is one Bub hook adapter backed by one Extism plug-in.
- `manifest` is a standard Extism manifest object.
- `wasi` stays on the Bub side because WASI enablement is a host/runtime decision.
- `hooks` maps Bub hook names to exported wasm functions.

## Runtime Model

- Bub still owns hook dispatch and precedence.
- `bub-extism` registers one Python adapter per configured entry.
- You can split hooks across multiple plug-ins or keep them in one module.

Typical layouts:

- one plug-in for `build_prompt`
- one plug-in for `run_model`
- one combined plug-in exporting both

## Supported Hooks

- `resolve_session`
- `build_prompt`
- `run_model`
- `run_model_stream`
- `load_state`
- `save_state`
- `render_outbound`
- `dispatch_outbound`
- `register_cli_commands`
- `onboard_config`
- `on_error`
- `system_prompt`
- `provide_tape_store`
- `provide_channels`
- `build_tape_context`

## CLI

`bub-extism` adds a management group similar to `bub mcp`:

```bash
bub extism list
bub extism show prompt
bub extism add prompt ./prompt.manifest.json --hook build_prompt=build_prompt
bub extism remove prompt
```

`bub extism add` expects:

- one standard Extism manifest JSON file
- one or more `--hook HOOK=EXPORT` bindings

If a wasm plug-in exposes `register_cli_commands`, its commands are registered
into the same `bub extism` group.

## Hook ABI Reference

Each exported hook function receives one UTF-8 JSON object.

`run_model` request:

```json
{
  "abi_version": "bub.extism.v1",
  "hook": "run_model",
  "args": {
    "prompt": "hello",
    "session_id": "cli:local",
    "state": {}
  }
}
```

`build_prompt` request:

```json
{
  "abi_version": "bub.extism.v1",
  "hook": "build_prompt",
  "args": {
    "message": {
      "content": "hello"
    },
    "session_id": "cli:local",
    "state": {}
  }
}
```

Bridge behavior:

- Bub runtime internals such as `_runtime_*` fields are removed from `state`
- Non-JSON-serializable values are skipped before the wasm call

Valid return shapes:

Plain text:

```text
hello from wasm
```

Wrapped value:

```json
{
  "value": "hello from wasm"
}
```

Skip current hook:

```json
{
  "skip": true
}
```

Return an error:

```json
{
  "error": {
    "message": "missing api key"
  }
}
```

## Descriptor Reference

`provide_channels` returns channel descriptors:

```json
{
  "value": [
    {
      "name": "wasm",
      "pollIntervalSeconds": 1,
      "functions": {
        "start": "channel_start",
        "poll": "channel_poll",
        "send": "channel_send",
        "stop": "channel_stop"
      }
    }
  ]
}
```

`provide_tape_store` returns a tape store descriptor:

```json
{
  "value": {
    "functions": {
      "list_tapes": "tape_list_tapes",
      "fetch_all": "tape_fetch_all",
      "append": "tape_append",
      "reset": "tape_reset"
    }
  }
}
```

`register_cli_commands` returns command descriptors:

```json
{
  "value": [
    {
      "name": "hello",
      "help": "Run the hello command.",
      "function": "cli_hello"
    }
  ]
}
```

That command is exposed as:

```bash
bub extism hello '{"name":"Bub"}'
```

## Examples

See [examples/README.md](./examples/README.md) for three verified paths:

- Rust `run_model` on its own
- Go `build_prompt` on its own
- Go `build_prompt` plus Rust `run_model` together

## Verification

From the repository root:

```bash
uv run --python 3.12 --no-project \
  --with-editable ./bub \
  --with-editable ./bub-contrib/packages/bub-extism \
  --with pytest \
  --with pytest-asyncio \
  -m pytest bub-contrib/packages/bub-extism/tests -q
```

To verify example builds and composition only:

```bash
uv run --python 3.12 --no-project \
  --with-editable ./bub \
  --with-editable ./bub-contrib/packages/bub-extism \
  --with pytest \
  --with pytest-asyncio \
  -m pytest bub-contrib/packages/bub-extism/tests/test_examples.py -q
```
