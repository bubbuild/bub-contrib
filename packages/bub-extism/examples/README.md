# bub-extism examples

This directory contains two verified example modules:

- `go-build-prompt`
- `rust-run-model`

They are intended to demonstrate three cases:

1. a single `build_prompt` wasm adapter
2. a single `run_model` wasm adapter
3. separate prompt and model adapters composed in one `extism.json`

## Prerequisites

Run these commands from the repository root.

Build prerequisites:

- Rust example:
  - `cargo`
  - `rustup`
  - `wasm32-unknown-unknown`
- Go example:
  - `go`
  - `GOOS=wasip1 GOARCH=wasm` support

The examples themselves do not require a real model backend.

## Build Artifacts

Build the Rust example:

```bash
cd bub-contrib/packages/bub-extism/examples/rust-run-model
cargo build --release --target wasm32-unknown-unknown
```

Expected artifact:

```text
bub-contrib/packages/bub-extism/examples/rust-run-model/target/wasm32-unknown-unknown/release/bub_extism_rust_run_model.wasm
```

Build the Go example:

```bash
cd bub-contrib/packages/bub-extism/examples/go-build-prompt
GOOS=wasip1 GOARCH=wasm go build -buildmode=c-shared -o go-build-prompt.wasm .
```

Expected artifact:

```text
bub-contrib/packages/bub-extism/examples/go-build-prompt/go-build-prompt.wasm
```

## Run the Rust `run_model` Example

This example exports `run_model` and returns:

```text
[rust-run-model:<session_id>] <prompt>
```

Example config:

```json
{
  "plugins": {
    "model": {
      "manifest": {
        "wasm": [
          {
            "path": "bub-contrib/packages/bub-extism/examples/rust-run-model/target/wasm32-unknown-unknown/release/bub_extism_rust_run_model.wasm"
          }
        ]
      },
      "hooks": {
        "run_model": "run_model"
      }
    }
  }
}
```

Verification:

```bash
uv run --python 3.12 --no-project \
  --with-editable ./bub \
  --with-editable ./bub-contrib/packages/bub-extism \
  --with pytest \
  --with pytest-asyncio \
  -m pytest bub-contrib/packages/bub-extism/tests/test_examples.py \
  -k rust_run_model_example_builds_and_runs -q
```

## Run the Go `build_prompt` Example

This example exports `build_prompt` and returns:

```text
[go-build-prompt:<session_id>] <message.content>
```

Example config:

```json
{
  "plugins": {
    "prompt": {
      "manifest": {
        "wasm": [
          {
            "path": "bub-contrib/packages/bub-extism/examples/go-build-prompt/go-build-prompt.wasm"
          }
        ]
      },
      "wasi": true,
      "hooks": {
        "build_prompt": "build_prompt"
      }
    }
  }
}
```

Verification:

```bash
uv run --python 3.12 --no-project \
  --with-editable ./bub \
  --with-editable ./bub-contrib/packages/bub-extism \
  --with pytest \
  --with pytest-asyncio \
  -m pytest bub-contrib/packages/bub-extism/tests/test_examples.py \
  -k go_build_prompt_example_builds_and_runs -q
```

## Run Both Examples Together

This is the composition case:

- Go handles `build_prompt`
- Rust handles `run_model`

Combined config:

```json
{
  "plugins": {
    "prompt": {
      "manifest": {
        "wasm": [
          {
            "path": "bub-contrib/packages/bub-extism/examples/go-build-prompt/go-build-prompt.wasm"
          }
        ]
      },
      "wasi": true,
      "hooks": {
        "build_prompt": "build_prompt"
      }
    },
    "model": {
      "manifest": {
        "wasm": [
          {
            "path": "bub-contrib/packages/bub-extism/examples/rust-run-model/target/wasm32-unknown-unknown/release/bub_extism_rust_run_model.wasm"
          }
        ]
      },
      "hooks": {
        "run_model": "run_model"
      }
    }
  }
}
```

Expected flow:

1. `build_prompt` returns `[go-build-prompt:example] hello from bub`
2. `run_model` receives that prompt and returns `[rust-run-model:example] [go-build-prompt:example] hello from bub`

Verification:

```bash
uv run --python 3.12 --no-project \
  --with-editable ./bub \
  --with-editable ./bub-contrib/packages/bub-extism \
  --with pytest \
  --with pytest-asyncio \
  -m pytest bub-contrib/packages/bub-extism/tests/test_examples.py \
  -k go_and_rust_examples_can_be_combined -q
```

## Full Example Verification

Run all three verified paths:

```bash
uv run --python 3.12 --no-project \
  --with-editable ./bub \
  --with-editable ./bub-contrib/packages/bub-extism \
  --with pytest \
  --with pytest-asyncio \
  -m pytest bub-contrib/packages/bub-extism/tests/test_examples.py -q
```
