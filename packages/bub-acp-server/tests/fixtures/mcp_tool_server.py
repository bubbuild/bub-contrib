import os

from fastmcp import FastMCP

server = FastMCP("acp-test")


@server.tool
def describe() -> str:
    return f"{os.getcwd()}:{os.environ.get('ACP_MCP_TEST', '')}"


@server.tool
def process_id() -> str:
    return str(os.getpid())


if __name__ == "__main__":
    server.run(transport="stdio", show_banner=False)
