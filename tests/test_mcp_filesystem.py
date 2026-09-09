"""
Unit test for build_filesystem_provider() -- checks it constructs the
MCPToolProvider correctly (command/args/risk_overrides) without actually
spawning npx, since the real server needs Node.js and touches the real
filesystem in ways CI/local test runs shouldn't depend on for something
this simple to verify statically.

Manually verified once against the real, official
@modelcontextprotocol/server-filesystem package: it starts, list_tools()
returns exactly the 14 tool names FILESYSTEM_RISK_OVERRIDES classifies
(10) or deliberately leaves unclassified (4 -- the write/edit/create/move
ones), a real read_text_file call against a file inside the configured
root succeeds, and a path outside that root is rejected by the server
itself (defense in depth on top of this codebase's own R0/R3 split).
"""
from dataclasses import dataclass

import pytest

pytest.importorskip("mcp")

from mcp_tools import FILESYSTEM_RISK_OVERRIDES, build_filesystem_provider  # noqa: E402


@dataclass
class _FakeConfig:
    mcp_filesystem_root: str
    mcp_startup_timeout_s: int = 90


def test_build_filesystem_provider_uses_npx_and_the_official_pinned_package():
    provider = build_filesystem_provider(_FakeConfig(mcp_filesystem_root="/tmp/some-folder"))

    assert provider._command == "npx"
    assert provider._args[0] == "-y"
    assert provider._args[1].startswith("@modelcontextprotocol/server-filesystem@")  # pinned


def test_build_filesystem_provider_passes_the_configured_root_as_the_only_allowed_directory():
    provider = build_filesystem_provider(_FakeConfig(mcp_filesystem_root="/tmp/some-folder"))

    assert provider._args[-1] == "/tmp/some-folder"
    assert provider._args.count("/tmp/some-folder") == 1  # exactly one allowed root, never more


def test_build_filesystem_provider_threads_through_the_configured_startup_timeout():
    provider = build_filesystem_provider(_FakeConfig(mcp_filesystem_root="/tmp/x", mcp_startup_timeout_s=42))
    assert provider._startup_timeout == 42


def test_read_and_list_tools_are_classified_r0():
    read_only_tools = {
        "read_file", "read_text_file", "read_media_file", "read_multiple_files", "list_directory",
        "list_directory_with_sizes", "directory_tree", "search_files", "get_file_info",
        "list_allowed_directories",
    }
    assert read_only_tools <= set(FILESYSTEM_RISK_OVERRIDES.keys())
    assert all(FILESYSTEM_RISK_OVERRIDES[t] == "R0" for t in read_only_tools)


def test_write_capable_tools_are_deliberately_left_unclassified():
    # This is meant to be a read-only arm: write_file/edit_file/
    # create_directory/move_file must NOT be in the R0 override dict, so
    # they fall through to ToolSpec's default (R3, always confirm) --
    # never silently enabled just because this server also offers them.
    write_tools = {"write_file", "edit_file", "create_directory", "move_file"}
    assert write_tools.isdisjoint(FILESYSTEM_RISK_OVERRIDES.keys())
