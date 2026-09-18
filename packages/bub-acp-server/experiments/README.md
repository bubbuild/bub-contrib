# ACP session MCP ablation

This experiment measures tool availability, session isolation and real stdio
process cleanup. It uses Bub's actual ACP, Agent and MCP paths, with a deterministic
model stub that records the tool schemas offered to the model. No external LLM,
API key or remote MCP service is required.

From the repository root:

```bash
uv run --locked python packages/bub-acp-server/experiments/mcp_ablation.py \
  --isolated --repeats 3 --jobs 2 --seed 20260918 \
  --output /tmp/acp-mcp-ablation.json
```

`--isolated` builds the local ACP/MCP packages in a disposable environment with
only their dependencies, pinned by `uv.lock`, including the locked Bub git commit.
Each observation runs in a fresh process and temporary workspace. Run order is
shuffled with the recorded seed. Without `--isolated`, the current interpreter
and installed plugins are used; this is suitable for a quick diagnostic only.

## Controls

All groups retain HTTP/SSE capability advertisement. Each ablation changes one
mechanism using a process-local patch; no production source is modified.

| Group | Change from full implementation |
| --- | --- |
| `full` | None |
| `capabilities_only` | Disable MCP connection bridge |
| `registry_only` | Replace session binding with writes to global `REGISTRY` |
| `shared_agent` | Reuse one runtime Agent across sessions |
| `no_replace_cleanup` | Skip previous channel cleanup on replacement |
| `no_shutdown_cleanup` | Skip current channel cleanup on shutdown |
| `context_exit_only` | Use `client.__aexit__()` instead of `client.close()` |

Each run creates and primes Agents for sessions A, B and an empty session before
MCP discovery, exercising Bub's snapshot of `REGISTRY` at Agent construction.
A and B then connect real stdio servers with the same server/tool names, different
identities and distinct extra tools. The sequence is A, B, A, empty, followed by
replacement of A with A2 and shutdown.

Four calls must return the expected server identity and working directory. Six
isolation observations check model-visible schemas for A/B/A/empty and tools on
ordinary Agents created before/after discovery. An exposed ordinary-Agent tool
is also invoked to verify that the leak is callable. Isolation counts absence of
unexpected tools, so an implementation exposing no tools can pass isolation while
failing every availability check; interpret both columns together.

Server PID files and `os.kill(pid, 0)` measure processes alive after replacement
and shutdown, with a one-second grace period at each boundary. Client connection
flags are recorded separately. Every group forcibly closes transports after
measurement and must leave no measured child alive. These POSIX checks target
macOS/Linux; this harness does not support Windows process groups.

Results are JSON with source/lock/harness provenance, installed versions and Bub
entry points, execution order, individual calls, schema observations and process
counts. [The report](results/report.md) explains the recorded run. The original
[pre-fix diagnostic](results/pre-fix.json) preserves the discovery that disconnected
clients could retain live stdio children; the formal `context_exit_only` group
reproduces that behavior under the same controls as the other groups.

This is a deterministic integration ablation, not a model quality or latency
benchmark. It does not test real HTTP/SSE servers, remote failures, sustained load
or prompt concurrency. Three repeats check repeatability, not statistical power.
