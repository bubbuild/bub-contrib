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

## Tool definitions and selection

In ordinary mode, MCP tools start as names and short summaries. `mcp_describe`
exposes selected native definitions on the next model call. Load missing tools
from any source together in one call; their original handlers remain unchanged.
Definitions follow first-discovery order in each tape context and survive restart.
New sessions, reset and handoff start fresh. Summaries stay stable as definitions
load. Builtins remain directly available; code mode uses its complete allowed stub.

Configure `mcp.allowed_tools` and `mcp.excluded_tools` in Bub's `config.yml`, or set
`BUB_MCP_ALLOWED_TOOLS` and `BUB_MCP_EXCLUDED_TOOLS` to JSON arrays. Patterns accept
runtime names (`mcp.notes_lookup`), model aliases (`mcp_notes_lookup`) and shell
wildcards. Exclusions win; an omitted allowlist allows all, `[]` allows none.
Restart Bub after changing configuration. Filtering limits discovery, native calls
and code mode; server discovery and listing remain unchanged.

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
discovery and binds the inventory and provider to the turn's Agent (including an explicit `_runtime_agent`). Remote
tools are selected per request without changing `Agent.tools`; comma commands resolve them
through the discovery inventory. Code mode loads the allowed catalog into its complete stub.
Stopping the channel removes its inventory, revealing any tools shadowed by that source.

An embedding application can call `channel.bind_agent(agent)` after startup discovery completes,
or `await channel.bind_runtime_tools(framework, message)` from its own `load_state` hook. Both paths
use instance tools. This requires Bub's tool-discovery API introduced in upstream PR #339.

The default `MCPChannel()` behavior remains backed by Bub's `mcp.json`. Read-only channels reject
`add()` and `remove()` so an embedding plugin remains the owner of its source configuration. The
standalone `MCPChannel()` construction remains unchanged. A composite plugin that must
keep unrelated components running when all MCP servers fail can subclass the channel and set
`stop_when_all_failed = False`.
