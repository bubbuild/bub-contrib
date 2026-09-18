"""A real stdio MCP server owned exclusively by the ablation harness."""

import json
import os
from pathlib import Path

from fastmcp import FastMCP

label = os.environ["ABLATION_LABEL"]
identity = {"label": label, "pid": os.getpid(), "cwd": os.getcwd()}
Path(os.environ["ABLATION_PID_FILE"]).write_text(json.dumps(identity))
server = FastMCP("acp-mcp-ablation")


@server.tool
def identify() -> str:
    """Return this server's identity to distinguish same-named session tools."""
    return json.dumps(identity)


@server.tool(name=f"only_{label}")
def session_only() -> str:
    """A session-specific tool used to measure visibility isolation."""
    return label


if __name__ == "__main__":
    server.run(transport="stdio", show_banner=False)
