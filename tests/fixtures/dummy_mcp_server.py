"""
A minimal, fully offline MCP server used only by test_mcp_tools.py.

Deliberately NOT the real mcp-server-fetch package: that one makes real
outbound HTTP requests (and, on first use, its readability dependency
does a one-time `npm install`), which is exactly the kind of slow/networked
dependency this project's test suite avoids -- see tests/conftest.py's
fixtures_server for the same reasoning applied to the browser arm. This
fixture proves MCPToolProvider's plumbing (start a server, discover its
tools, call one, get the result back, shut down cleanly) without needing
the real fetch server or any network access.

Run directly: `python dummy_mcp_server.py` (talks MCP over stdio).
"""
import os

from mcp.server.fastmcp import FastMCP

app = FastMCP("dummy-test-server")


@app.tool()
def echo(text: str) -> str:
    """Echo back the given text."""
    return f"echo: {text}"


@app.tool()
def env_echo(var_name: str) -> str:
    """Echo back the value of an environment variable this process sees --
    used to prove MCPToolProvider's `env` argument is actually threaded
    through to the spawned subprocess (see build_brave_search_provider(),
    which passes BRAVE_API_KEY this same way)."""
    return os.environ.get(var_name, "")


@app.tool()
def mystery() -> str:
    """A tool with no risk_overrides entry in the test -- used to prove
    unclassified MCP tools default to R3 (always confirm)."""
    return "mystery executed"


if __name__ == "__main__":
    app.run()
