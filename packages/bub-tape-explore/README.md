# bub-tape-explore

Tape exploration tools for `bub`.

## What It Provides

- A Bub tool named `tape.map`
- A Bub tool named `tape.explore`
- A Bub tool named `tape.window`
- Plain-text tape summaries and raw windows suitable for model/tool consumption

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-tape-explore"
```

## Runtime Behavior

- `tape.map` returns a bounded list of structural nodes for the current tape
- `tape.explore` returns `before`, `after`, and `stats` for selected nodes
- `tape.window` returns raw entries for one node and supports `limit` and `filter`
- Node boundaries come from native tape structure such as `anchor`, `loop.step.start`, `loop.step`, and contiguous `run_id` clusters
- `tape.window` is the source-of-truth view for exact tape content
