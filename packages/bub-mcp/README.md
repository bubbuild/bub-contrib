# bub-mcp

Expose configured MCP servers as Bub tools.

## Configuration

The plugin reads MCP server definitions from Bub's default config file:

- `~/.bub/config.yml`
- or `$BUB_HOME/config.yml` when `BUB_HOME` is set

Add `mcp_servers` to that file:

```yaml
mcp_servers:
  weather:
    url: https://weather.example.com/mcp
    transport: http
  local:
    command: python
    args:
      - ./server.py
```

## Runtime Behavior

`bub-mcp` uses a dedicated lifecycle channel to schedule FastMCP bootstrap in `Channel.start()`
and release resources in `Channel.stop()`. Once the background task finishes, discovered remote
tools are registered into Bub's global tool registry with the
`mcp.` prefix, for example `mcp.weather_get_forecast`.

The lifecycle channel also exposes runtime config management helpers:

- `list()`: return the current `mcp_servers` mapping from Bub config
- `add(name, server)`: persist one server config and reload the MCP client
- `remove(name)`: delete one server config and reload the MCP client
