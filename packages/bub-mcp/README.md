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

Discovered MCP tools are registered as Bub deferred tools by default: the model only sees their
names until it loads their definitions with `tool.describe`, which keeps large MCP tool sets out of
the prompt. Set `BUB_MCP_DEFERRED_TOOLS=false` to expose them directly instead.

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

## Lifecycle

The plugin owns `mcp.json` connections through Bub's `provide_lifespan` hook, so every entry point
that enters `framework.running()` (`bub gateway`, `bub chat`, `bub run`, `bub acp`, ...) connects
the configured servers. Discovery runs in the background; the first turn waits for it before binding
tools. Servers that fail to connect are reported by the `mcp` tool and do not stop Bub.

## Embedding

Other Bub plugins can reuse the MCP connection and tool bridge with a read-only configuration:

```python
from bub_mcp.plugin import MCPManager

manager = MCPManager.from_server_configs(
    {"weather": {"url": "https://weather.example.com/mcp"}}
)

# Inside the embedding application's async lifecycle, e.g. its own provide_lifespan hook:
await manager.connect()
try:
    manager.bind_agent(agent)
    # Run the agent while its MCP connections are open.
finally:
    await manager.stop()
```

`start()` begins discovery in the background instead of waiting for it like `connect()`.
Discovered tools belong to the manager and are available through `manager.tools`; they are not
provided through the `provide_tools` hook. Bub collects those tools when a `BubAgent` is created, so
tools discovered afterward would not reach existing agents. The plugin's `load_state` hook waits for startup
discovery and binds tools to the turn's Agent (including an explicit `_runtime_agent`). Stopping
the manager removes its bindings and restores any tools it replaced.

An embedding application can call `manager.bind_agent(agent)` after startup discovery completes,
or `await manager.bind_runtime_tools(framework, message)` from its own `load_state` hook. Both paths
use instance tools. This requires Bub's instance-tool API introduced in upstream PR #311.

The default `MCPManager()` behavior remains backed by Bub's `mcp.json`. Read-only managers reject
`add()` and `remove()` so an embedding plugin remains the owner of its source configuration.
`MCPChannel` remains as a deprecated alias of `MCPManager`; it is no longer a Bub channel.
