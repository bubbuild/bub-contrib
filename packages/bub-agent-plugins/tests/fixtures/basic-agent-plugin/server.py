from fastmcp import FastMCP

server = FastMCP("basic-agent-plugin")


@server.tool
def ping(value: str = "ok") -> str:
    """Return a deterministic value for the isolated MCP check."""
    return f"basic-mcp-ok:{value}"


if __name__ == "__main__":
    server.run(show_banner=False)
