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
import concurrent.futures
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
#
# 90s for startup specifically because an npx-launched server (e.g. Brave
# Search) can do a real network round-trip to the npm registry on every
# invocation even when the package is already cached locally, unrelated to
# anything this code does -- measured as low as ~1s and as high as 70s+ for
# the exact same command back to back while building this. A pip-installed
# server (fetch) starts near-instantly regardless, so this only costs
# anything on the slow path it's meant to tolerate.
STARTUP_TIMEOUT_S = 90
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
        env: dict[str, str] | None = None, startup_timeout: float = STARTUP_TIMEOUT_S,
    ):
        self._command = command
        self._args = args or []
        self._risk_overrides = risk_overrides or {}
        # Extra environment variables for the subprocess -- e.g. an API key
        # a server reads from its own environment (see
        # build_brave_search_provider() below). Passed straight through to
        # StdioServerParameters, which merges it with the current process's
        # environment rather than replacing it (so PATH etc. still resolve).
        self._env = env
        # Overridable per instance (see config.py's MCP_STARTUP_TIMEOUT_S)
        # because how long is "reasonable" here depends on the server and
        # the user's own network, not just this code -- see
        # STARTUP_TIMEOUT_S's module-level comment for the real-world
        # range this was measured against.
        self._startup_timeout = startup_timeout

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
            self._run(self._start_session(), timeout=self._startup_timeout)
        except Exception as e:
            self._stop_loop()
            # concurrent.futures.TimeoutError (and asyncio's own alias for
            # it) both stringify to "" -- str(e) alone would silently
            # produce an empty, useless "WHY:" line for the single most
            # likely failure mode (a slow npx registry round-trip; see
            # STARTUP_TIMEOUT_S's comment), so it needs its own message.
            if isinstance(e, (TimeoutError, concurrent.futures.TimeoutError)):
                cause = (
                    f"It did not finish starting within {self._startup_timeout}s. If this keeps happening on a "
                    "slow connection, raise MCP_STARTUP_TIMEOUT_S in .env."
                )
            else:
                cause = str(e) or f"{type(e).__name__} (no further detail)."
            raise TaskCannotBeCompleted(
                explain(
                    f"The MCP server ('{self._command} {' '.join(self._args)}') could not be started.",
                    cause,
                    "Check that the command is installed and on PATH, that any required API key is set, "
                    "and that you have a working internet connection, then try again.",
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
        # `text` being falsy is only "nothing to report" when there were no
        # content blocks at all -- a tool whose real, meaningful result IS
        # an empty string (e.g. a search that legitimately found nothing)
        # must not have that silently swapped for a generic placeholder.
        if not result.content:
            return f"MCP tool '{tool.name}' returned no content."
        return text

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
        params = StdioServerParameters(command=self._command, args=self._args, env=self._env)
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
    """Factory for the fetch MCP server -- see ARCHITECTURE_DECISIONS.md
    section 4. Adding a second server means adding a second factory like
    this one (see build_brave_search_provider() below), not changing
    MCPToolProvider itself."""
    return MCPToolProvider(
        command=config.mcp_fetch_command, args=[], risk_overrides=FETCH_SERVER_RISK_OVERRIDES,
        startup_timeout=config.mcp_startup_timeout_s,
    )


# Reviewed once, by us: every one of these is a read-only search/lookup
# call against Brave's Search API (web/local/video/image/news/place
# search, an AI summarizer, and an "LLM context" helper) -- none of them
# write, delete, or have any side effect beyond the API request itself,
# so all are R0, the same reasoning as the fetch server's one tool. Tool
# names confirmed directly against the real server's list_tools() output;
# a future server version adding a new tool would NOT automatically
# inherit R0 -- it falls through to R3 until reviewed and added here.
#
# Deliberately @brave/brave-search-mcp-server (published by Brave itself,
# actively maintained), NOT @modelcontextprotocol/server-brave-search --
# that one is the same "official reference server" family as the fetch
# server this project already uses, but it's been marked deprecated
# ("Package no longer supported") on npm; installing a known-unsupported
# package as a new dependency isn't worth it when Brave publishes and
# maintains their own replacement.
BRAVE_SEARCH_RISK_OVERRIDES: dict[str, RiskLevel] = {
    "brave_web_search": "R0",
    "brave_local_search": "R0",
    "brave_video_search": "R0",
    "brave_image_search": "R0",
    "brave_news_search": "R0",
    "brave_summarizer": "R0",
    "brave_llm_context": "R0",
    "brave_place_search": "R0",
}


def build_brave_search_provider(config) -> MCPToolProvider:
    """Factory for the second MCP server this project wires up: Brave's
    own web/local/video/image/news search, via npx (it's an npm package --
    Node.js must be installed, the same real-world dependency the fetch
    server's bundled Readability engine already has). The API key is
    passed as an env var rather than a CLI arg so it doesn't show up in a
    local process listing (`ps`/Task Manager).

    Pinned to the exact version BRAVE_SEARCH_RISK_OVERRIDES was reviewed
    against -- an unpinned `npx -y` would silently pick up whatever a
    future release adds. That's not a safety hole on its own (an
    unreviewed new tool still falls through to R3 -- see this module's
    docstring), but there's no reason to let the tool list drift out from
    under a fixed risk review when pinning costs nothing.
    """
    return MCPToolProvider(
        command="npx", args=["-y", "@brave/brave-search-mcp-server@2.1.3"],
        env={"BRAVE_API_KEY": config.brave_api_key}, risk_overrides=BRAVE_SEARCH_RISK_OVERRIDES,
        startup_timeout=config.mcp_startup_timeout_s,
    )


# Reviewed once, by us: the read/list/search/info tools have no side
# effect beyond reading from disk, so they're R0 -- directly analogous to
# excel_read_cell/excel_read_range. write_file, edit_file, create_directory,
# and move_file are deliberately NOT listed here: this is meant to be a
# read-only arm (the same "safest proving ground" reasoning as fetch and
# Brave Search), so those fall through to ToolSpec's default, R3 -- always
# confirmed, never silently enabled by a misconfigured .env. A user who
# genuinely wants the agent writing arbitrary local files can still say
# yes to that confirmation each time; nothing here hard-blocks it.
# `read_file` is the server's own deprecated alias for `read_text_file`
# (identical read-only behavior) -- classified the same for consistency,
# in case an older model habit or a future server version still reaches
# for the old name.
FILESYSTEM_RISK_OVERRIDES: dict[str, RiskLevel] = {
    "read_file": "R0",
    "read_text_file": "R0",
    "read_media_file": "R0",
    "read_multiple_files": "R0",
    "list_directory": "R0",
    "list_directory_with_sizes": "R0",
    "directory_tree": "R0",
    "search_files": "R0",
    "get_file_info": "R0",
    "list_allowed_directories": "R0",
}


def build_filesystem_provider(config) -> MCPToolProvider:
    """Factory for the third MCP server this project wires up: read-only
    access to one local directory the user explicitly opts into via
    MCP_FILESYSTEM_ROOT (see config.py -- there is deliberately no default
    directory; a wrong guess at "somewhere safe" is not this codebase's
    call to make). The official, actively-maintained
    @modelcontextprotocol/server-filesystem enforces that same boundary
    itself too (a path outside the given root is rejected server-side,
    confirmed by actually trying it), so this is defense in depth, not the
    only thing standing between the model and the rest of the disk.

    Pinned to the exact version FILESYSTEM_RISK_OVERRIDES was reviewed
    against, same reasoning as build_brave_search_provider().
    """
    return MCPToolProvider(
        command="npx", args=["-y", "@modelcontextprotocol/server-filesystem@2026.8.31", config.mcp_filesystem_root],
        risk_overrides=FILESYSTEM_RISK_OVERRIDES, startup_timeout=config.mcp_startup_timeout_s,
    )
