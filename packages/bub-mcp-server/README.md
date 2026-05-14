# bub-mcp-server

Expose Bub as an SSE MCP server.

## What It Provides

- Channel implementation: `MCPServerChannel` (`name = "mcp-server"`)
- FastMCP SSE server lifecycle managed by Bub channel startup and shutdown
- One MCP tool: `run_model`

## Tool

`run_model` accepts:

- `prompt` (required): input text to send through Bub
- `session_id` (optional): Bub session id, default `mcp:default`

It returns Bub's `model_output` for that turn.

## Configuration

Settings are read from environment variables with the `BUB_MCP_SERVER_` prefix.

- `BUB_MCP_SERVER_HOST`: bind host, default `127.0.0.1`
- `BUB_MCP_SERVER_PORT`: bind port, default `28280` (BUBU0 on 9-keyboard)
- `BUB_MCP_SERVER_PATH`: SSE path, default `/sse`
- `BUB_MCP_SERVER_LOG_LEVEL`: Uvicorn log level, default `info`

## Installation

```bash
uv pip install "git+https://github.com/bubbuild/bub-contrib.git#subdirectory=packages/bub-mcp-server"
```

In this repository, the package is included in the workspace and root dependencies.

## Usage

Start Bub with channels enabled. The MCP SSE endpoint is available at:

```text
http://127.0.0.1:28280/sse
```

Configure your MCP client to use SSE transport against that URL.
