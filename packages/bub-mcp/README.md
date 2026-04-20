# bub-mcp

Expose configured MCP servers as Bub tools.

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
