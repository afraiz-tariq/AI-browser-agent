"""
MCP (Model Context Protocol) arm: wraps one MCP server, launched as a
subprocess and spoken to over stdio, as a ToolProvider -- so any MCP
server's tools show up in the same flat tool list as the browser/Excel
arms, with no special-casing anywhere else in the codebase. This is the
third arm from ARCHITECTURE_DECISIONS.md section 4, added additively:
nothing about the browser/Excel arms changes, and the whole thing is off
by default (see config.py's ENABLE_MCP_FETCH).

Risk classification is OURS, not the server's. A server's own tool
descriptions are not a trust boundary -- an MCP server (especially a
third-party one) could describe a destructive tool as harmless, or add a
new tool in a future version that this code has never seen. So every tool
this provider exposes gets its risk tier from RISK OVERRIDES that WE
define per tool name below the ToolProvider subclass, reviewed once by
reading what the tool actually does; anything not explicitly listed there
falls through to ToolSpec's own default, R3 (always confirm). See
tool_provider.py's module docstring for why R3 is deliberately not
configurable off.

The MCP Python SDK is async-only (built on anyio); this codebase's agent
loop (agent.py) is synchronous end to end, and turning it async just for
this one arm isn't worth the churn. To bridge that, this module runs one
dedicated background thread with its own asyncio event loop for the
lifetime of a provider instance, and every public method blocks on that
loop via run_coroutine_threadsafe(...).result() -- the rest of the
codebase never sees an awaitable.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import AsyncExitStack
from typing import Any

from errors import TaskCannotBeCompleted, explain
from tool_provider import RiskLevel, ToolProvider, ToolSpec

# How long to wait for the server to start up (subprocess spawn + MCP
# initialize handshake) and for any single tool call to complete, in
# seconds. A hung server (or a network-bound tool like fetch stuck on a
# slow site) should eventually surface as a clear error, not a silently
# frozen task.
STARTUP_TIMEOUT_S = 30
CALL_TIMEOUT_S = 60


class MCPToolError(Exception):
    """Raised when an MCP tool call reports isError=True."""


class MCPToolProvider(ToolProvider):
    """
    Wraps one MCP server as a ToolProvider. Each of the server's own tools
    is exposed to the model under a "mcp_" prefix (e.g. the fetch server's
    "fetch" tool becomes "mcp_fetch") so it can never collide with a
    browser/Excel tool name.

    `risk_overrides` maps the server's OWN tool names (not the prefixed
    ones) to a RiskLevel we've explicitly reviewed and assigned; any tool
    the server exposes that isn't in this dict is UNCLASSIFIED and gets
    ToolSpec's default, R3 -- see module docstring.

    Tool specs are only known once the server has actually started and
    answered list_tools (unlike the browser/Excel arms, whose specs are
    static), so get_tool_specs() starts the server on first call rather
    than waiting for a separate ensure_ready() -- see get_tool_specs().
    """

    def __init__(
        self, command: str, args: list[str] | None = None, risk_overrides: dict[str, RiskLevel] | None = None,
    ):
        self._command = command
        self._args = args or []
        self._risk_overrides = risk_overrides or {}

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._session = None
        # exposed name ("mcp_<tool>") -> mcp.types.Tool, populated once by
        # ensure_ready()'s tool discovery.
        self._tools_by_exposed_name: dict[str, Any] = {}

    def ensure_ready(self) -> None:
        if self._session is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        try:
            self._run(self._start_session(), timeout=STARTUP_TIMEOUT_S)
        except Exception as e:
            self._stop_loop()
            raise TaskCannotBeCompleted(
                explain(
                    f"The MCP server ('{self._command}') could not be started.",
                    str(e),
                    "Check that it's installed and on PATH (e.g. `pip install mcp-server-fetch`), "
                    "and that MCP_FETCH_COMMAND in .env points at it.",
                )
            ) from e

    def get_tool_specs(self) -> list[ToolSpec]:
        # Specs are discovered dynamically from the live server, so this
        # doubles as the lazy-start trigger -- called once per task from
        # agent.py's provider-registration loop, same as every other arm.
        self.ensure_ready()
        return [
            ToolSpec(
                name=exposed_name,
                description=tool.description or f"MCP tool '{tool.name}' (no description provided).",
                properties=(tool.inputSchema or {}).get("properties", {}),
                required=(tool.inputSchema or {}).get("required", []),
                risk_level=self._risk_overrides.get(tool.name, "R3"),
            )
            for exposed_name, tool in self._tools_by_exposed_name.items()
        ]

    def execute(self, name: str, args: dict) -> str | None:
        tool = self._tools_by_exposed_name[name]
        result = self._run(self._session.call_tool(tool.name, args), timeout=CALL_TIMEOUT_S)
        text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        if result.isError:
            raise MCPToolError(text or f"MCP tool '{tool.name}' reported an error with no details.")
        return text or f"MCP tool '{tool.name}' returned no content."

    def describe_for_confirmation(self, name: str, args: dict) -> str:
        tool = self._tools_by_exposed_name.get(name)
        return f"run MCP tool '{tool.name if tool else name}' with {args}"

    def close(self) -> None:
        """Shuts down the session and subprocess cleanly. Must be called
        once the task is done (see agent.py's run_task finally block) --
        a no-op if the server was never actually started."""
        if self._loop is None:
            return
        try:
            self._run(self._exit_stack.aclose(), timeout=STARTUP_TIMEOUT_S)
        except Exception:
            pass  # best-effort cleanup; the task's own outcome doesn't depend on this
        self._stop_loop()

    def _run(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _stop_loop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop = None
        self._thread = None
        self._session = None

    async def _start_session(self) -> None:
        # Imported lazily so the rest of the codebase (and its tests) work
        # without the `mcp` package installed unless this arm is actually used.
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._exit_stack = AsyncExitStack()
        params = StdioServerParameters(command=self._command, args=self._args)
        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        listed = await session.list_tools()
        self._tools_by_exposed_name = {f"mcp_{tool.name}": tool for tool in listed.tools}


# Reviewed once, by us, not the server: what does this specific tool
# actually do? "fetch" (the official MCP reference "fetch" server's only
# tool) is a read-only HTTP GET + HTML-to-text extraction -- directly
# analogous to the browser arm's "goto" (also R0), so it's safe to never
# confirm. A future version of this same server adding a new tool would
# NOT automatically inherit this -- it would fall through to R3 (see
# MCPToolProvider's docstring) until reviewed and added here.
FETCH_SERVER_RISK_OVERRIDES: dict[str, RiskLevel] = {
    "fetch": "R0",
}


def build_fetch_provider(config) -> MCPToolProvider:
    """Factory for the one MCP server this project currently wires up --
    see ARCHITECTURE_DECISIONS.md section 4 ("one read-only server first").
    Adding a second server later means adding a second factory like this
    one, not changing MCPToolProvider itself."""
    return MCPToolProvider(
        command=config.mcp_fetch_command, args=[], risk_overrides=FETCH_SERVER_RISK_OVERRIDES,
    )
