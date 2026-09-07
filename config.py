"""
Configuration loading for the AI browser agent.

Everything the agent needs to know about *how* to run (which LLM, how many
steps it may take, whether to ask before risky actions, etc.) lives in a
.env file so that nobody has to edit code -- and so API keys never end up
hard-coded in source that might get committed to git.
"""
import os
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


@dataclass
class Config:
    # --- LLM provider (kept provider-agnostic so the model can be swapped
    # later without touching agent.py -- see llm.py) ---
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "openai").lower())
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

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

    # --- Discord bot interface (discord_bot.py) ---
    discord_bot_token: str = field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    discord_allowed_user_id: int = field(default_factory=lambda: _int("DISCORD_ALLOWED_USER_ID", 0))

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
        if self.max_steps < 1:
            problems.append("MAX_STEPS must be at least 1.")
        return problems


def load_config() -> Config:
    return Config()
