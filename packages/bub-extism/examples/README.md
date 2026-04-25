# bub-extism examples

These examples show how to implement Bub extensions in languages other than
Python while still using Bub's pluggy-based extension surface through
`bub-extism`.

## Rust model stream

`rust-model-stream` mirrors model-provider plugins such as `bub-kimi` and
`bub-codex`. It implements `run_model_stream` and returns Republic stream
events.

Build:

```bash
cd packages/bub-extism/examples/rust-model-stream
cargo build --release --target wasm32-unknown-unknown
```

Configure:

```json
{
  "defaultPlugin": "rust-model-stream",
  "plugins": {
    "rust-model-stream": {
      "wasmPath": "packages/bub-extism/examples/rust-model-stream/target/wasm32-unknown-unknown/release/bub_extism_rust_model_stream.wasm",
      "hooks": {
        "run_model_stream": "run_model_stream"
      }
    }
  }
}
```

## Go channel

`go-channel` mirrors channel plugins such as `bub-discord`, `bub-feishu`, and
`bub-wecom`. It implements `provide_channels` and a `send` function used by the
Python `ExtismChannel` proxy.

Build:

```bash
cd packages/bub-extism/examples/go-channel
GOOS=wasip1 GOARCH=wasm go build -buildmode=c-shared -o go-channel.wasm .
```

Configure:

```json
{
  "defaultPlugin": "go-channel",
  "plugins": {
    "go-channel": {
      "wasmPath": "packages/bub-extism/examples/go-channel/go-channel.wasm",
      "wasi": true,
      "hooks": {
        "provide_channels": "provide_channels"
      }
    }
  }
}
```
