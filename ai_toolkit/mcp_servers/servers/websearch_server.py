from mcp.server.fastmcp import FastMCP
import os
from ai_toolkit.mcp_servers.tools.web_search import web_search

mcp = FastMCP("tavily_search")

@mcp.tool()
def websearch_server(query: str)-> dict:
    """
    Search the web using Tavily.

    Args:
        query: Search query.

    Returns:
        Tavily search response.
    """
    return web_search(query)


if __name__ == "__main__":
    mcp.run()

