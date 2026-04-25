# bub-extism

`bub-extism` lets a Bub workspace run selected Bub hooks through an
[Extism](https://extism.org/) WebAssembly plug-in.

The package is intentionally a bridge, not a replacement for Bub's pluggy
extension model. Bub still discovers `bub-extism` as a normal Python plugin,
then `bub-extism` delegates configured hook calls to a `.wasm` module written
with any Extism PDK language.

The Extism Python runtime is installed with this package. The dependency is
kept on the verified `extism` `1.1.x` and `extism-sys` `1.12.x` lines because
the Python package includes native runtime wheels.

## Supported Hooks

The bridge exposes the current Bub standard hook surface:

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

Pure value hooks map directly to WebAssembly calls. Hooks that return Python
runtime objects use Python-side proxies:

- `run_model_stream` accepts a returned list of stream events and wraps it as
  `AsyncStreamEvents`.
- `provide_channels` accepts channel descriptors and creates `ExtismChannel`
  proxies.
- `provide_tape_store` accepts a tape store descriptor and creates an
  `ExtismTapeStore` proxy.
- `register_cli_commands` accepts command descriptors and registers them under
  `bub extism`.
- `build_tape_context` accepts a declarative context object; arbitrary Python
  selector callbacks are not part of the WASM ABI.

## Configuration

Create `~/.bub/extism.json`:

```json
{
  "defaultPlugin": "echo",
  "plugins": {
    "echo": {
      "wasmPath": "/absolute/path/to/plugin.wasm",
      "wasi": false,
      "config": {
        "model": "demo"
      },
      "hooks": {
        "resolve_session": "resolve_session",
        "build_prompt": "build_prompt",
        "run_model": "run_model",
        "run_model_stream": "run_model_stream",
        "load_state": "load_state",
        "save_state": "save_state",
        "render_outbound": "render_outbound",
        "dispatch_outbound": "dispatch_outbound",
        "register_cli_commands": "register_cli_commands",
        "onboard_config": "onboard_config",
        "on_error": "on_error",
        "system_prompt": "system_prompt"
      }
    }
  }
}
```

You can also load a URL or a full Extism manifest:

```json
{
  "defaultPlugin": "remote",
  "plugins": {
    "remote": {
      "wasmUrl": "https://example.com/plugin.wasm",
      "hooks": {
        "run_model": "run_model"
      }
    }
  }
}
```

```json
{
  "defaultPlugin": "manifest",
  "plugins": {
    "manifest": {
      "manifest": {
        "wasm": [
          {
            "url": "https://example.com/plugin.wasm",
            "hash": "sha256..."
          }
        ],
        "allowed_hosts": ["api.example.com"]
      },
      "hooks": {
        "run_model": "run_model"
      }
    }
  }
}
```

Use `BUB_EXTISM_CONFIG_PATH=/path/to/extism.json` to override the config path.

## Hook ABI

Each exported hook function receives one UTF-8 JSON object.

For `run_model`:

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

For `system_prompt`:

```json
{
  "abi_version": "bub.extism.v1",
  "hook": "system_prompt",
  "args": {
    "prompt": "hello",
    "state": {}
  }
}
```

The bridge removes Bub runtime internals such as `_runtime_agent` and
non-JSON-serializable values from `state` before calling WebAssembly.

The wasm function may return plain text:

```text
hello from wasm
```

Or a JSON object:

```json
{
  "value": "hello from wasm"
}
```

It can skip the hook:

```json
{
  "skip": true
}
```

Or return an error:

```json
{
  "error": {
    "message": "missing api key"
  }
}
```

For compatibility with early demos, `{"run_model": "..."}` and
`{"system_prompt": "..."}` are still accepted.

## Proxy Descriptors

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

The command is exposed as `bub extism hello '{"name":"Bub"}'`.

## Development

From the repository root:

```bash
uv run --directory packages/bub-extism --with ../bub --with pytest --with pytest-asyncio pytest
```

These tests use a fake Extism module and do not require a local WebAssembly
runtime.
