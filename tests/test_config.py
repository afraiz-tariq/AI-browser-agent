"""
Tests for config.py -- the .env parsing helpers (_bool, _int) and
Config.validate(), which gates whether `python agent.py` / discord_bot.py
even start. Had no dedicated test file before this one; only exercised
indirectly through conftest.py's test_config fixture, which never touches
validate() or these helpers' edge cases (an unset var, an invalid value,
mixed case, ...) at all.
"""
import pytest

from config import Config, _bool, _int


# --- _bool -------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True), ("  true  ", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("off", False), ("garbage", False), ("", False),
])
def test_bool_parses_common_truthy_and_falsy_strings(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_FLAG", raw)
    assert _bool("SOME_FLAG", not expected) is expected


def test_bool_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert _bool("SOME_FLAG", True) is True
    assert _bool("SOME_FLAG", False) is False


# --- _int ----------------------------------------------------------------

def test_int_parses_a_valid_integer_string(monkeypatch):
    monkeypatch.setenv("SOME_NUMBER", "42")
    assert _int("SOME_NUMBER", 0) == 42


def test_int_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_NUMBER", raising=False)
    assert _int("SOME_NUMBER", 7) == 7


def test_int_falls_back_to_default_when_empty(monkeypatch):
    monkeypatch.setenv("SOME_NUMBER", "")
    assert _int("SOME_NUMBER", 7) == 7


def test_int_falls_back_to_default_on_invalid_value(monkeypatch):
    monkeypatch.setenv("SOME_NUMBER", "not-a-number")
    assert _int("SOME_NUMBER", 7) == 7


def test_int_accepts_the_literal_zero_rather_than_treating_it_as_falsy(monkeypatch):
    # Regression-shaped: `int(val) if val else default` reads the *string*
    # "0" for truthiness, not the parsed number, so this must NOT fall
    # through to the default -- but it's exactly the kind of off-by-logic
    # a future edit to this helper could reintroduce.
    monkeypatch.setenv("SOME_NUMBER", "0")
    assert _int("SOME_NUMBER", 99) == 0


# --- Config.validate() ----------------------------------------------------

def test_validate_passes_for_a_well_formed_mock_config():
    assert Config(llm_provider="mock", max_steps=5).validate() == []


def test_validate_flags_missing_openai_key():
    problems = Config(llm_provider="openai", openai_api_key="", max_steps=5).validate()
    assert any("OPENAI_API_KEY" in p for p in problems)


def test_validate_passes_with_openai_key_present():
    problems = Config(llm_provider="openai", openai_api_key="sk-test", max_steps=5).validate()
    assert problems == []


def test_validate_flags_missing_anthropic_key():
    problems = Config(llm_provider="anthropic", anthropic_api_key="", max_steps=5).validate()
    assert any("ANTHROPIC_API_KEY" in p for p in problems)


def test_validate_flags_unknown_provider():
    problems = Config(llm_provider="not-a-real-provider", max_steps=5).validate()
    assert any("Unknown LLM_PROVIDER" in p for p in problems)


@pytest.mark.parametrize("max_steps", [0, -1, -100])
def test_validate_flags_non_positive_max_steps(max_steps):
    problems = Config(llm_provider="mock", max_steps=max_steps).validate()
    assert any("MAX_STEPS" in p for p in problems)


def test_validate_flags_brave_search_enabled_without_an_api_key():
    problems = Config(llm_provider="mock", max_steps=5, enable_mcp_brave_search=True, brave_api_key="").validate()
    assert any("BRAVE_API_KEY" in p for p in problems)


def test_validate_passes_with_brave_search_enabled_and_a_key_set():
    problems = Config(
        llm_provider="mock", max_steps=5, enable_mcp_brave_search=True, brave_api_key="a-real-looking-key",
    ).validate()
    assert problems == []


def test_validate_does_not_require_a_brave_key_when_brave_search_is_disabled():
    problems = Config(llm_provider="mock", max_steps=5, enable_mcp_brave_search=False, brave_api_key="").validate()
    assert problems == []


def test_validate_can_report_multiple_problems_at_once():
    problems = Config(llm_provider="openai", openai_api_key="", max_steps=0).validate()
    assert len(problems) >= 2


def test_validate_flags_filesystem_enabled_without_a_root():
    problems = Config(llm_provider="mock", max_steps=5, enable_mcp_filesystem=True, mcp_filesystem_root="").validate()
    assert any("MCP_FILESYSTEM_ROOT" in p for p in problems)


def test_validate_flags_filesystem_root_that_does_not_exist():
    problems = Config(
        llm_provider="mock", max_steps=5, enable_mcp_filesystem=True,
        mcp_filesystem_root="/this/path/definitely/does/not/exist/anywhere",
    ).validate()
    assert any("not an existing directory" in p for p in problems)


def test_validate_passes_with_filesystem_enabled_and_a_real_directory(tmp_path):
    problems = Config(
        llm_provider="mock", max_steps=5, enable_mcp_filesystem=True, mcp_filesystem_root=str(tmp_path),
    ).validate()
    assert problems == []


def test_validate_does_not_require_a_filesystem_root_when_disabled():
    problems = Config(llm_provider="mock", max_steps=5, enable_mcp_filesystem=False, mcp_filesystem_root="").validate()
    assert problems == []


def test_voice_mode_defaults_to_tap_and_rejects_typos(monkeypatch):
    monkeypatch.delenv("VOICE_MODE", raising=False)
    assert Config(llm_provider="mock").voice_mode == "tap"
    monkeypatch.setenv("VOICE_MODE", " Hold ")
    assert Config(llm_provider="mock").voice_mode == "hold"
    monkeypatch.setenv("VOICE_MODE", "tapp")
    assert any("VOICE_MODE" in p for p in Config(llm_provider="mock").validate())
