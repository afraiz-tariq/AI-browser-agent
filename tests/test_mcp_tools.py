"""
Unit tests for the MCP arm (mcp_tools.py), fully offline: these launch
tests/fixtures/dummy_mcp_server.py (a tiny local MCP server with no
network access of its own) rather than the real mcp-server-fetch package,
for the same reason test_browser.py uses a local fixtures_server instead
of the real internet -- see that fixture's docstring.

Requires the optional `mcp` package (see requirements.txt) -- skipped
entirely if it isn't installed, since MCP is an opt-in arm (ENABLE_MCP_FETCH
defaults to false) and the rest of the suite must not depend on it.
"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp_tools import MCPToolError, MCPToolProvider  # noqa: E402
from tool_provider import requires_confirmation  # noqa: E402

DUMMY_SERVER = Path(__file__).parent / "fixtures" / "dummy_mcp_server.py"


@pytest.fixture()
def provider():
    p = MCPToolProvider(
        command=sys.executable, args=[str(DUMMY_SERVER)], risk_overrides={"echo": "R0"},
    )
    yield p
    p.close()


def test_get_tool_specs_starts_the_server_and_discovers_its_tools(provider):
    specs = {s.name: s for s in provider.get_tool_specs()}
    assert "mcp_echo" in specs
    assert "mcp_mystery" in specs
    # Tool names are exposed with an "mcp_" prefix so they can never
    # collide with a browser/Excel tool name.
    assert specs["mcp_echo"].required == ["text"]


def test_reviewed_tool_gets_its_explicit_risk_override(provider):
    specs = {s.name: s for s in provider.get_tool_specs()}
    assert specs["mcp_echo"].risk_level == "R0"


def test_unreviewed_tool_defaults_to_r3_always_confirm(provider):
    # "mystery" is not in risk_overrides -- proves an MCP server tool this
    # code hasn't explicitly reviewed is treated as unsafe-by-default, not
    # silently trusted just because the server itself calls it harmless.
    specs = {s.name: s for s in provider.get_tool_specs()}
    assert specs["mcp_mystery"].risk_level == "R3"
    misconfigured_config = type("C", (), {"confirm_sensitive_actions": False, "confirm_r1_actions": False})()
    assert requires_confirmation(specs["mcp_mystery"].risk_level, misconfigured_config) is True


def test_execute_calls_the_underlying_tool_and_returns_its_text(provider):
    provider.get_tool_specs()  # starts the server
    result = provider.execute("mcp_echo", {"text": "hello world"})
    assert result == "echo: hello world"


def test_execute_unknown_tool_raises_keyerror(provider):
    provider.get_tool_specs()
    with pytest.raises(KeyError):
        provider.execute("mcp_does_not_exist", {})


def test_describe_for_confirmation_uses_the_original_unprefixed_tool_name(provider):
    provider.get_tool_specs()
    desc = provider.describe_for_confirmation("mcp_mystery", {})
    assert "mystery" in desc


def test_ensure_ready_is_idempotent(provider):
    provider.ensure_ready()
    first = provider.get_tool_specs()
    provider.ensure_ready()  # must not restart the server or error
    second = provider.get_tool_specs()
    assert {s.name for s in first} == {s.name for s in second}


def test_close_before_ensure_ready_is_a_safe_no_op():
    p = MCPToolProvider(command=sys.executable, args=[str(DUMMY_SERVER)])
    p.close()  # must not raise even though the server was never started


def test_starting_a_nonexistent_command_raises_task_cannot_be_completed():
    from errors import TaskCannotBeCompleted

    p = MCPToolProvider(command="this-command-does-not-exist-anywhere", args=[])
    with pytest.raises(TaskCannotBeCompleted):
        p.ensure_ready()
    p.close()
