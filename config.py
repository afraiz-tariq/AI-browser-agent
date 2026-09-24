"""
Configuration loading for the AI browser agent.

Everything the agent needs to know about *how* to run (which LLM, how many
steps it may take, whether to ask before risky actions, etc.) lives in a
.env file so that nobody has to edit code -- and so API keys never end up
hard-coded in source that might get committed to git.
"""
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load the .env file that sits next to this file (if present). This must
# happen before we read any os.environ values below.
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

LOGS_DIR = PROJECT_ROOT / "logs"
OUTPUT_DIR = PROJECT_ROOT / "output"
LOGS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    val = os.getenv(name)
    try:
        return float(val) if val else default
    except ValueError:
        return default


@dataclass
class Config:
    # --- LLM provider (kept provider-agnostic so the model can be swapped
    # later without touching agent.py -- see llm.py) ---
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "openai").lower())
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    # How many times the OpenAI/Anthropic SDK retries a request itself
    # (connection errors, timeouts, 429s, 5xx) before giving up and raising
    # -- passed straight to the SDK client, which already implements
    # backoff/jitter correctly. 2 matches both SDKs' own default, so this
    # is a no-op unless someone opts into a higher value via .env.
    llm_max_retries: int = field(default_factory=lambda: _int("LLM_MAX_RETRIES", 2))

    # --- Optional faster decider for browser steps (jev.py) ---
    # "claude" (default): every step is decided by the LLM above, as always.
    # "hybrid": TypeSafe's Jev picks click/type/scroll steps from the page's
    # own elements; everything else (and anything Jev is unsure of) still
    # goes to the LLM above. See docs/JEV_VOICE_PLAN.md.
    decider: str = field(default_factory=lambda: os.getenv("DECIDER", "claude").strip().lower())
    typesafe_api_key: str = field(default_factory=lambda: os.getenv("TYPESAFE_API_KEY", ""))
    typesafe_model: str = field(default_factory=lambda: os.getenv("TYPESAFE_MODEL", "jev-latest"))
    # Below this confidence (operation x target), Jev's pick is not used and
    # Claude decides the step instead.
    jev_min_confidence: float = field(default_factory=lambda: _float("JEV_MIN_CONFIDENCE", 0.5))

    # --- Cost / runaway-loop controls ---
    max_steps: int = field(default_factory=lambda: _int("MAX_STEPS", 20))
    step_timeout_ms: int = field(default_factory=lambda: _int("STEP_TIMEOUT_MS", 15000))
    max_dom_chars: int = field(default_factory=lambda: _int("MAX_DOM_CHARS", 6000))

    # --- Browser behaviour ---
    headless: bool = field(default_factory=lambda: _bool("HEADLESS", False))
    chrome_channel: str = field(default_factory=lambda: os.getenv("CHROME_CHANNEL", "chrome"))
    chrome_executable_path: str = field(default_factory=lambda: os.getenv("CHROME_EXECUTABLE_PATH", ""))
    chrome_user_data_dir: str = field(
        default_factory=lambda: os.getenv("CHROME_USER_DATA_DIR") or str(PROJECT_ROOT / "chrome_profile")
    )
    use_persistent_profile: bool = field(default_factory=lambda: _bool("USE_PERSISTENT_PROFILE", True))

    # --- Safety ---
    confirm_sensitive_actions: bool = field(default_factory=lambda: _bool("CONFIRM_SENSITIVE_ACTIONS", True))
    # R1 (reversible, in-memory-only writes, e.g. excel_write_cell) confirm
    # only if this is explicitly turned on -- off by default, since undoing
    # them is as simple as not saving/persisting. See tool_provider.py.
    confirm_r1_actions: bool = field(default_factory=lambda: _bool("CONFIRM_R1_ACTIONS", False))

    # --- Discord bot interface (discord_bot.py) ---
    discord_bot_token: str = field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    discord_allowed_user_id: int = field(default_factory=lambda: _int("DISCORD_ALLOWED_USER_ID", 0))

    # --- MCP (Model Context Protocol) arm -- see mcp_tools.py ---
    # Off by default: this is an additive third arm (ARCHITECTURE_DECISIONS.md
    # section 4), not something every task should suddenly depend on.
    enable_mcp_fetch: bool = field(default_factory=lambda: _bool("ENABLE_MCP_FETCH", False))
    mcp_fetch_command: str = field(default_factory=lambda: os.getenv("MCP_FETCH_COMMAND", "mcp-server-fetch"))
    # Second MCP server: Brave web/local/video/image/news search (also
    # read-only, also off by default). Needs Node.js/npx and a free key
    # from https://brave.com/search/api/.
    enable_mcp_brave_search: bool = field(default_factory=lambda: _bool("ENABLE_MCP_BRAVE_SEARCH", False))
    brave_api_key: str = field(default_factory=lambda: os.getenv("BRAVE_API_KEY", ""))
    # How long to wait for an MCP server to start (subprocess spawn + MCP
    # initialize handshake) before giving up. 90s by default -- generous
    # because an npx-launched server can do a real network round-trip to
    # the npm registry on every invocation even when already cached
    # locally; raise this further if that's consistently too slow on your
    # connection. See mcp_tools.py's STARTUP_TIMEOUT_S for the measured
    # real-world range this was chosen against.
    mcp_startup_timeout_s: int = field(default_factory=lambda: _int("MCP_STARTUP_TIMEOUT_S", 90))
    # Third MCP server: read-only access to ONE local directory (also off
    # by default). Deliberately no default directory -- guessing at
    # "somewhere safe" on the user's disk is not this codebase's call to
    # make; the user names an exact folder they're comfortable exposing.
    enable_mcp_filesystem: bool = field(default_factory=lambda: _bool("ENABLE_MCP_FILESYSTEM", False))
    mcp_filesystem_root: str = field(default_factory=lambda: os.getenv("MCP_FILESYSTEM_ROOT", ""))

    # --- Windows desktop automation arm (windows_tools.py) ---
    # Off by default (ARCHITECTURE_DECISIONS.md section 5): far more
    # open-ended/brittle than the browser (DOM) or Excel (file format)
    # arms, so every mutating windows_* action is R3 (always confirms)
    # regardless of this flag or CONFIRM_SENSITIVE_ACTIONS/CONFIRM_R1_ACTIONS
    # -- see windows_tools.py and tool_provider.py's requires_confirmation().
    enable_windows_automation: bool = field(default_factory=lambda: _bool("ENABLE_WINDOWS_AUTOMATION", False))

    def validate(self) -> list[str]:
        """Return a list of human-readable problems, empty if config is OK."""
        problems = []
        if self.llm_provider == "openai" and not self.openai_api_key:
            problems.append(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            problems.append(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        if self.llm_provider not in ("openai", "anthropic", "mock"):
            problems.append(
                f"Unknown LLM_PROVIDER '{self.llm_provider}'. Supported: openai, anthropic, mock."
            )
        if self.decider not in ("claude", "hybrid"):
            problems.append(f"Unknown DECIDER '{self.decider}'. Supported: claude, hybrid.")
        if self.decider == "hybrid" and not self.typesafe_api_key:
            problems.append(
                "DECIDER=hybrid needs TYPESAFE_API_KEY (from console.typesafe.ai) in .env, "
                "or set DECIDER=claude."
            )
        if not 0 <= self.jev_min_confidence <= 1:
            problems.append("JEV_MIN_CONFIDENCE must be between 0 and 1.")
        if self.max_steps < 1:
            problems.append("MAX_STEPS must be at least 1.")
        if self.enable_mcp_brave_search and not self.brave_api_key:
            problems.append(
                "ENABLE_MCP_BRAVE_SEARCH is true but BRAVE_API_KEY is not set. "
                "Get a free key at https://brave.com/search/api/."
            )
        if self.enable_mcp_filesystem:
            if not self.mcp_filesystem_root:
                problems.append(
                    "ENABLE_MCP_FILESYSTEM is true but MCP_FILESYSTEM_ROOT is not set. "
                    "Set it to the one local folder you want the agent able to read from."
                )
            elif not Path(self.mcp_filesystem_root).is_dir():
                problems.append(f"MCP_FILESYSTEM_ROOT '{self.mcp_filesystem_root}' is not an existing directory.")
        if self.enable_windows_automation and sys.platform != "win32":
            problems.append(
                "ENABLE_WINDOWS_AUTOMATION is true, but this is not Windows (sys.platform != 'win32'). "
                "The Windows desktop automation arm only works on Windows."
            )
        return problems


def load_config() -> Config:
    return Config()
