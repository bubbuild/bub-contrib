import os

from fastmcp import FastMCP

server = FastMCP("acp-test")


@server.tool
def describe() -> str:
    return f"{os.getcwd()}:{os.environ.get('ACP_MCP_TEST', '')}"


if __name__ == "__main__":
    server.run(transport="stdio", show_banner=False)
