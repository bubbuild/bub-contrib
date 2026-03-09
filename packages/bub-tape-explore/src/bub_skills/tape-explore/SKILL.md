---
name: tape-explore
description: Explore the current Bub tape through structural nodes and raw windows. Use when the current tape is too long to read linearly, when Bub needs to find a relevant run or anchor before opening raw entries, or when Bub should inspect tape structure with `tape.map`, preview a few candidate nodes with `tape.explore`, and then read original entries with `tape.window`.
---

# Tape Explore

Use the tools as a narrowing workflow:

1. Call `tape.map` to see a bounded list of recent structural nodes.
2. Call `tape.explore` to inspect `before`, `after`, and `stats` for a small set of nodes.
3. Call `tape.window` to read original entries for one chosen node.
4. Use `tape.window limit=...` or `tape.window filter=...` when the node is still too large.

Keep these boundaries:

- Treat `tape.map` as structure only. It shows node boundaries, not evidence.
- Treat `tape.explore` as a preview only. It helps choose a node, not replace the source tape.
- Use `tape.window` before making factual conclusions, quoting content, or reasoning about exact tool behavior.
- Prefer small limits and inspect only a few nodes at a time.
- Skip directly to `tape.window` if the target node is already known.
