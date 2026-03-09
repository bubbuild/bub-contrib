# bub-tape-explore

Tape exploration tools for `bub`.

## What It Provides

- `tape.map`: compress the current tape into a bounded list of structural nodes
- `tape.explore`: show `before`, `after`, and `stats` for selected nodes
- `tape.window`: render raw entries for one node

## Design Notes

- Uses native `TapeQuery` / `TapeStore` semantics only
- No sidecar storage
- No numeric scoring
- Assumes `republic > 0.5.4` and current `bub` mainline runtime
- Node boundaries come from native tape structure such as:
  - `anchor`
  - `loop.step.start` / `loop.step`
  - contiguous `run_id` clusters

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-tape-explore"
```
