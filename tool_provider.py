"""
The contract every "arm" (browser, excel, and later mcp) implements, so
agent.py's loop and llm.py's tool-building code don't need to know which
arm a given action belongs to. Before this existed, agent.py dispatched by
checking `if action.startswith("excel_")` and llm.py imported each arm's
spec dict by name directly -- workable at two arms, but every new arm
(MCP servers, eventually Windows) would have meant one more hardcoded
special case in both places. This file replaces that with one interface
both arms already implement identically (see BrowserToolProvider in
browser.py and ExcelToolProvider in excel_tools.py).

This is a refactor, not new functionality: every confirmation prompt,
every risk decision, every VERIFY check should behave exactly as it did
before. See ARCHITECTURE_DECISIONS.md section 4 for why this exists and
what it unlocks next (MCP as a third provider, additive).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# R0 = read-only / no side effect              -> never confirms
# R1 = reversible write (e.g. in-memory only)   -> confirms only if explicitly configured on
# R2 = external side effect (disk write, form   -> confirms by default (existing CONFIRM_SENSITIVE_ACTIONS)
#      submission, message sent, ...)
# R3 = sensitive/destructive/UNCLASSIFIED       -> ALWAYS confirms, not configurable off
RiskLevel = Literal["R0", "R1", "R2", "R3"]


@dataclass
class ToolSpec:
    name: str  # must be API-safe: matches what's sent to the LLM as a tool name, [a-zA-Z0-9_-]+ only
    description: str
    properties: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    # Deliberately safe-by-default: a provider must explicitly opt a tool
    # down to R0-R2. A future MCPProvider wrapping a third-party server
    # that adds a new tool it hasn't classified should never silently be
    # treated as safe just because it forgot to set this.
    risk_level: RiskLevel = "R3"


class ToolProvider:
    """Base interface every arm implements."""

    def get_tool_specs(self) -> list[ToolSpec]:
        raise NotImplementedError

    def ensure_ready(self) -> None:
        """Called once before execute() on every dispatch -- lazily start
        whatever underlying resource this provider needs (a browser
        process, a subprocess for a future MCP server, ...). Default:
        no-op, for providers with nothing to start (Excel/openpyxl has no
        process). Should raise errors.TaskCannotBeCompleted on failure to
        start, since that's a hard stop for the whole task, not a single
        retryable action."""
        return None

    def execute(self, name: str, args: dict) -> str | None:
        """Run the tool. Return a short human-readable result string if the
        arm has no other way to report it back to the model (see
        excel_tools.py's ExcelToolProvider); return None if the effect will
        be picked up some other way (see browser.py's OBSERVE + VERIFY)."""
        raise NotImplementedError

    def get_dynamic_risk(self, name: str, args: dict) -> RiskLevel | None:
        """Override the static risk_level at call time, for tools whose
        real risk depends on the specific arguments -- e.g. a browser click
        is only actually risky if the target element looks like a submit/
        delete/purchase button, which can't be known from the tool name
        alone. Return None to use the static risk_level unchanged."""
        return None

    def describe_for_confirmation(self, name: str, args: dict) -> str:
        """Human-readable fragment used in the '[y/n]' prompt, e.g.
        "click <button 'Submit'>. This looks like it may have side
        effects" -- the caller wraps it as f"Ready to {this}. Continue?".
        Override for wording specific to the arm; the default is a
        generic fallback that's still usable, just less friendly."""
        return f"run {name} with {args}"

    def verify(self, name: str, args: dict, pre_state: Any, post_state: Any) -> str | None:
        """Return a warning string if the action doesn't look like it
        worked, else None. Optional -- default is 'no opinion'. Only the
        browser arm currently has anything meaningful to say here (see
        browser.py); Excel actions are deterministic and already report
        their result directly via execute()'s return value."""
        return None

    def wants_verification(self, name: str, args: dict) -> bool:
        """Whether the agent loop should schedule a VERIFY check (see
        verify() above) after this action runs. Default False -- most
        actions either can't be ambiguous (Excel: deterministic, self-
        reporting) or don't reliably change any observable state (a plain
        scroll). Override where "did it actually work" is genuinely
        unclear from the return value alone."""
        return False


def requires_confirmation(risk_level: RiskLevel, config) -> bool:
    """The one place that turns a risk tier into an actual [y/n] decision.
    R3 is intentionally NOT gated by any config flag -- an unclassified or
    explicitly dangerous tool must always ask, so a misconfigured .env
    can't silently disable protection for it."""
    if risk_level == "R0":
        return False
    if risk_level == "R1":
        return bool(getattr(config, "confirm_r1_actions", False))
    if risk_level == "R3":
        return True
    return bool(config.confirm_sensitive_actions)  # R2
