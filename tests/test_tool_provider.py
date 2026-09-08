"""
Unit tests for the ToolProvider contract itself (tool_provider.py) --
independent of any real arm, so these test the risk-tier policy directly
rather than through browser.py/excel_tools.py's specific tools.

The property that matters most here: an unclassified/R3 tool must always
confirm, even if the rest of the config is set up to disable confirmations
everywhere else. That's the fail-safe a future MCPProvider (a third-party
server whose new tools haven't been risk-classified yet) depends on.
"""
from types import SimpleNamespace

from tool_provider import RiskLevel, ToolProvider, ToolSpec, requires_confirmation


def _config(confirm_sensitive_actions: bool, confirm_r1_actions: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        confirm_sensitive_actions=confirm_sensitive_actions, confirm_r1_actions=confirm_r1_actions,
    )


def test_r0_never_confirms_regardless_of_config():
    assert requires_confirmation("R0", _config(confirm_sensitive_actions=True, confirm_r1_actions=True)) is False
    assert requires_confirmation("R0", _config(confirm_sensitive_actions=False)) is False


def test_r1_confirms_only_when_explicitly_enabled():
    assert requires_confirmation("R1", _config(confirm_sensitive_actions=True, confirm_r1_actions=False)) is False
    assert requires_confirmation("R1", _config(confirm_sensitive_actions=True, confirm_r1_actions=True)) is True


def test_r2_follows_confirm_sensitive_actions():
    assert requires_confirmation("R2", _config(confirm_sensitive_actions=True)) is True
    assert requires_confirmation("R2", _config(confirm_sensitive_actions=False)) is False


def test_r3_always_confirms_even_if_everything_else_is_turned_off():
    # This is the fail-safe: a misconfigured .env (both confirm flags off)
    # must not silently disable protection for an unclassified/dangerous tool.
    misconfigured = _config(confirm_sensitive_actions=False, confirm_r1_actions=False)
    assert requires_confirmation("R3", misconfigured) is True


def test_toolspec_defaults_to_r3_when_not_explicitly_set():
    spec = ToolSpec(name="mystery_tool", description="An unclassified tool.")
    assert spec.risk_level == "R3"


class FakeProvider(ToolProvider):
    """A minimal provider standing in for a future arm (e.g. MCP) -- proves
    the base ToolProvider defaults are safe on their own, with no overrides."""

    def get_tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(name="fake_read", description="read something", risk_level="R0"),
            ToolSpec(name="fake_unclassified", description="a tool that forgot to set risk_level"),
        ]

    def execute(self, name: str, args: dict) -> str | None:
        return f"executed {name}"


def test_fake_provider_r0_tool_never_confirms():
    provider = FakeProvider()
    spec = {s.name: s for s in provider.get_tool_specs()}["fake_read"]
    assert requires_confirmation(spec.risk_level, _config(confirm_sensitive_actions=True)) is False


def test_fake_provider_unclassified_tool_always_confirms():
    provider = FakeProvider()
    spec = {s.name: s for s in provider.get_tool_specs()}["fake_unclassified"]
    assert spec.risk_level == "R3"
    misconfigured = _config(confirm_sensitive_actions=False, confirm_r1_actions=False)
    assert requires_confirmation(spec.risk_level, misconfigured) is True


def test_default_get_dynamic_risk_is_none_and_describe_for_confirmation_has_a_fallback():
    provider = FakeProvider()
    assert provider.get_dynamic_risk("fake_read", {}) is None
    assert "fake_read" in provider.describe_for_confirmation("fake_read", {"x": 1})


def test_default_verify_and_wants_verification_are_inert():
    provider = FakeProvider()
    assert provider.wants_verification("fake_read", {}) is False
    assert provider.verify("fake_read", {}, pre_state=None, post_state=None) is None


def test_get_dynamic_risk_override_takes_precedence_over_static_risk_level():
    class ArgSensitiveProvider(FakeProvider):
        def get_dynamic_risk(self, name: str, args: dict) -> RiskLevel | None:
            if name == "fake_read" and args.get("dangerous"):
                return "R2"
            return None

    provider = ArgSensitiveProvider()
    spec = {s.name: s for s in provider.get_tool_specs()}["fake_read"]
    assert spec.risk_level == "R0"  # static tier is still R0
    assert provider.get_dynamic_risk("fake_read", {"dangerous": True}) == "R2"
    assert provider.get_dynamic_risk("fake_read", {"dangerous": False}) is None
