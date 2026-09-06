"""
Shared pytest fixtures.

Tests run against a local static file server (tests/fixtures/) instead of
the real internet, both because this keeps tests fast/free and because
some environments (e.g. sandboxed CI containers) block general outbound
web traffic entirely.
"""
import functools
import http.server
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def test_config(tmp_path):
    """A Config pointed at the sandbox-bundled Chromium, headless, no persistent profile."""
    import os

    chrome_path = os.environ.get("TEST_CHROME_EXECUTABLE_PATH", "")
    return Config(
        llm_provider="mock",
        headless=True,
        chrome_executable_path=chrome_path,
        use_persistent_profile=False,
        confirm_sensitive_actions=True,
        max_steps=10,
        step_timeout_ms=10000,
    )
