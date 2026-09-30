# bub-mcp

Expose configured MCP servers as Bub tools.

## Installation

```bash
uv pip install bub-mcp
```

## Configuration

The plugin reads MCP server definitions from a dedicated JSON file under Bub home:

- `~/.bub/mcp.json`
- or `$BUB_HOME/mcp.json` when `BUB_HOME` is set

The file must contain a top-level `mcpServers` mapping:

```json
{
  "mcpServers": {
    "weather": {
      "url": "https://weather.example.com/mcp",
      "transport": "http"
    },
    "local": {
      "command": "python",
      "args": ["./server.py"]
    }
  }
}
```

## Tool definitions

In ordinary tool mode, MCP tools initially appear as names and short summaries.
The model uses `mcp_describe` to obtain selected native definitions, then calls
the original tools normally. Definitions are reused within the current tape
context, including after restart. Code mode continues to use its Python stub.
New sessions, tape reset, and handoff start discovery again.
MCP owns this policy; ordinary Bub tools need no loading flag. This requires a
Bub version supporting `Agent.tool_providers`.

## Tool selection

Limit the remote tools bound to Agents through Bub's `config.yml`:

```yaml
mcp:
  allowed_tools:
    - mcp.lody_session_list
    - mcp.lody_session_history
    - mcp.time_*
  excluded_tools:
    - mcp.time_convert_time
```

The equivalent environment variables are `BUB_MCP_ALLOWED_TOOLS` and
`BUB_MCP_EXCLUDED_TOOLS`, each containing a JSON array of name patterns. Patterns
are case-sensitive and accept shell wildcards such as `*` and `?`. Use runtime
names (`mcp.lody_session_list`) or model aliases (`mcp_lody_session_list`).
Exclusions take precedence. Omitting `allowed_tools` allows all discovered tools;
an empty list exposes none. Restart Bub after changing configuration.

Selection applies to both direct model calls and code mode, including embedded
MCP channels. It does not change tool descriptions or parameter schemas. Servers
still connect and discover their complete tool catalogs; the operator's
`bub mcp list` command shows that catalog, while the agent's `mcp` tool lists only
allowed tools. Selection also limits the summaries and definitions available
through `mcp_describe`; deferred exposure does not bypass these restrictions.

## CLI Usage

Use the CLI to inspect and manage `mcp.json`:

```bash
bub mcp list
```

Add an HTTP server:

```bash
bub mcp add --transport http weather https://weather.example.com/mcp
```

Add an SSE server with headers:

```bash
bub mcp add \
  --transport sse \
  --header "Authorization: Bearer token" \
  events \
  https://events.example.com/mcp
```

Add a stdio server with environment variables:

```bash
bub mcp add \
  --transport stdio \
  --env API_KEY=secret \
  filesystem \
  -- npx -y @modelcontextprotocol/server-filesystem /tmp
```

Remove a server:

```bash
bub mcp remove weather
```

`bub mcp add` writes the server config into `mcp.json` and performs a connection test before exiting.

## Embedding

Other Bub plugins can reuse the MCP lifecycle and tool bridge with a read-only configuration:

```python
from bub_mcp.plugin import MCPChannel

channel = MCPChannel.from_server_configs(
    {"weather": {"url": "https://weather.example.com/mcp"}}
)

# Inside the embedding application's async lifecycle:
await channel.connect()
try:
    channel.bind_agent(agent)
    # Run the agent while its MCP connections are open.
finally:
    await channel.stop()
```

Discovered tools belong to the channel and are available through `channel.tools`; discovery does
not modify `bub.tools.REGISTRY`. Bub snapshots that registry when an `Agent` is created, so changing
it afterward would not update existing agents. The plugin's `load_state` hook waits for startup
discovery and binds tools to the turn's Agent (including an explicit `_runtime_agent`). Stopping
the channel removes its bindings and restores any tools it replaced.

An embedding application can call `channel.bind_agent(agent)` after startup discovery completes,
or `await channel.bind_runtime_tools(framework, message)` from its own `load_state` hook. Both paths
use instance tools. This requires Bub's instance-tool API introduced in upstream PR #311.

The default `MCPChannel()` behavior remains backed by Bub's `mcp.json`. Read-only channels reject
`add()` and `remove()` so an embedding plugin remains the owner of its source configuration. The
standalone `MCPChannel()` construction remains unchanged. A composite plugin that must
keep unrelated components running when all MCP servers fail can subclass the channel and set
`stop_when_all_failed = False`.
