# bub-acp

`bub-acp` adds ACP support in both directions:

- `bub acp serve` runs Bub as an ACP agent over stdio.
- the plugin can use an external ACP agent as Bub's `run_model` / `run_model_stream` backend.

## Configuration

Outbound ACP agents are stored in `~/.bub/acp.json`.

```json
{
  "defaultAgent": "kimi",
  "agents": {
    "kimi": {
      "command": "toad",
      "args": ["agent", "start"],
      "env": {
        "KIMI_API_KEY": "..."
      }
    }
  }
}
```

## CLI

```bash
bub acp list
bub acp add --default kimi -- toad agent start
bub acp serve
```

## Verification

From the `bub-contrib` repository root, run the package tests with real ACP subprocess transport:

```bash
uv run --directory packages/bub-acp --with ../bub --with ../republic --with pytest --with pytest-asyncio pytest
```
