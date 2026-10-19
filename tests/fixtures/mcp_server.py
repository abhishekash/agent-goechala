"""Small stdio MCP server for framework interoperability tests."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("observatory-fixture", version="1.0")


@server.tool()
def lookup_release(project: str) -> str:
    """Return the fixture release status."""
    return f"{project}: ready"


if __name__ == "__main__":
    server.run(transport="stdio")
