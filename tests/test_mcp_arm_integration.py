"""
Integration tests for the MCP arm running through the full agent loop
(agent.py's run_task), proving the third-arm wiring actually works end to
end -- not just MCPToolProvider in isolation (see test_mcp_tools.py).

Uses the same offline dummy_mcp_server.py fixture as test_mcp_tools.py,
swapped in for the real fetch server via monkeypatching
agent.build_fetch_provider -- run_task() itself is never told which
command to launch, so this proves the registry/dispatch wiring in agent.py
without depending on the real mcp-server-fetch package's network behavior.
"""
import dataclasses
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("mcp")

from agent import run_task  # noqa: E402
from llm import LLMClient, MockProvider  # noqa: E402
from mcp_tools import MCPToolProvider  # noqa: E402

DUMMY_SERVER = Path(__file__).parent / "fixtures" / "dummy_mcp_server.py"


def _reply(thought, action, args):
    return json.dumps({"thought": thought, "action": action, "args": args, "confidence": "high"})


def _mcp_enabled_config(test_config):
    return dataclasses.replace(test_config, enable_mcp_fetch=True)


def test_mcp_only_task_calls_a_reviewed_r0_tool_without_confirming(test_config, monkeypatch):
    monkeypatch.setattr(
        "agent.build_fetch_provider",
        lambda config: MCPToolProvider(
            command=sys.executable, args=[str(DUMMY_SERVER)], risk_overrides={"echo": "R0"},
        ),
    )
    confirm = MagicMock(return_value=True)
    mock = MockProvider([
        _reply("Calling the MCP echo tool.", "mcp_echo", {"text": "hello from a test"}),
        _reply("Done.", "finish", {"summary": "The MCP tool echoed back the text."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task(
        "Call the MCP echo tool.", _mcp_enabled_config(test_config), dry_run=False,
        llm_client=llm_client, confirm_callback=confirm,
    )

    assert outcome["success"] is True
    confirm.assert_not_called()  # echo is explicitly reviewed as R0 -- never confirms


def test_unreviewed_mcp_tool_always_confirms_even_with_confirmations_off(test_config, monkeypatch):
    monkeypatch.setattr(
        "agent.build_fetch_provider",
        lambda config: MCPToolProvider(
            command=sys.executable, args=[str(DUMMY_SERVER)], risk_overrides={"echo": "R0"},
        ),
    )
    # confirm_sensitive_actions=False would normally mean "never confirm R2
    # actions" -- proving the unclassified/R3 "mystery" tool still confirms
    # anyway is the whole point of the R0-R3 fail-safe (see tool_provider.py).
    config = dataclasses.replace(_mcp_enabled_config(test_config), confirm_sensitive_actions=False)
    confirm = MagicMock(return_value=False)
    mock = MockProvider([
        _reply("Calling the unreviewed MCP tool.", "mcp_mystery", {}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task(
        "Call the unreviewed MCP tool.", config, dry_run=False, llm_client=llm_client, confirm_callback=confirm,
    )

    assert confirm.called
    assert outcome["success"] is False
    assert "declined" in outcome["result"].lower()


def test_mcp_disabled_by_default_leaves_mcp_tools_unavailable(test_config):
    # ENABLE_MCP_FETCH defaults to False -- a task shouldn't see mcp_* tools
    # (or pay any startup cost for them) unless explicitly turned on.
    assert test_config.enable_mcp_fetch is False
    mock = MockProvider([_reply("No MCP tool exists.", "mcp_echo", {"text": "x"})])
    llm_client = LLMClient(mock)

    outcome = run_task("Try to call an MCP tool that isn't registered.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is False
