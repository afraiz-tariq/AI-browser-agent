"""
Unit test for build_brave_search_provider() -- checks it constructs the
MCPToolProvider correctly (command/args/env/risk_overrides) without
actually spawning npx or hitting Brave's real API, since that needs
Node.js, network access, and a real paid/free-tier API key that CI/local
test runs shouldn't depend on.

The mechanism this relies on (env passthrough, R0 classification, MCP
wiring in general) is covered end-to-end offline in test_mcp_tools.py and
test_mcp_arm_integration.py via the dummy server; this file only pins
build_brave_search_provider()'s own construction, which those don't touch.

Manually verified once against the real, official @brave/brave-search-mcp-server
package (not the deprecated @modelcontextprotocol/server-brave-search) with
a placeholder API key: it starts, and list_tools() returns exactly the 8
tool names BRAVE_SEARCH_RISK_OVERRIDES classifies below.
"""
from dataclasses import dataclass

import pytest

pytest.importorskip("mcp")

from mcp_tools import BRAVE_SEARCH_RISK_OVERRIDES, build_brave_search_provider  # noqa: E402


@dataclass
class _FakeConfig:
    brave_api_key: str
    mcp_startup_timeout_s: int = 90


def test_build_brave_search_provider_uses_npx_and_the_official_pinned_package():
    provider = build_brave_search_provider(_FakeConfig(brave_api_key="test-key-123"))

    assert provider._command == "npx"
    assert provider._args[0] == "-y"
    assert provider._args[1].startswith("@brave/brave-search-mcp-server@")  # pinned, not just the bare package name


def test_build_brave_search_provider_passes_the_api_key_as_an_env_var_not_a_cli_arg():
    provider = build_brave_search_provider(_FakeConfig(brave_api_key="test-key-123"))

    assert provider._env == {"BRAVE_API_KEY": "test-key-123"}
    assert "test-key-123" not in provider._args  # never a bare CLI arg -- see build_brave_search_provider()'s docstring


def test_build_brave_search_provider_threads_through_the_configured_startup_timeout():
    provider = build_brave_search_provider(_FakeConfig(brave_api_key="test-key-123", mcp_startup_timeout_s=42))
    assert provider._startup_timeout == 42


def test_all_eight_known_brave_tools_are_classified_r0():
    expected_tools = {
        "brave_web_search", "brave_local_search", "brave_video_search", "brave_image_search",
        "brave_news_search", "brave_summarizer", "brave_llm_context", "brave_place_search",
    }
    assert set(BRAVE_SEARCH_RISK_OVERRIDES.keys()) == expected_tools
    assert all(risk == "R0" for risk in BRAVE_SEARCH_RISK_OVERRIDES.values())
