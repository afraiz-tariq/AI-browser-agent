"""
Tests for AnthropicProvider's own decide() logic in llm.py -- parsing a
real SDK response into the {"action", "thought", "args"} shape the agent
loop expects, and turning SDK failures into LLMError. See
test_llm_providers_openai.py for the OpenAI equivalent (split into its own
file since `openai` is an optional dependency and `anthropic` isn't).

Had no test coverage before this file: every existing test drives the
loop through MockProvider instead, which never exercises this parsing
code at all. These tests construct a real AnthropicProvider (a real
anthropic.Anthropic client, which makes no network call by itself) and
monkeypatch only the one network-calling method (messages.create) with a
fake response object shaped like the real SDK's, so the actual parsing
logic runs for real.
"""
from types import SimpleNamespace

import pytest

from llm import AnthropicProvider, LLMError
from tool_provider import ToolSpec

SOME_TOOL_SPECS = [ToolSpec(name="finish", description="Finish.", properties={}, required=[], risk_level="R0")]


def _provider():
    return AnthropicProvider(api_key="test-key", model="claude-test", tool_specs=SOME_TOOL_SPECS)


def _response(*blocks, input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        content=list(blocks), usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name, input_dict):
    return SimpleNamespace(type="tool_use", name=name, input=input_dict)


def test_parses_a_tool_use_block():
    provider = _provider()
    response = _response(_tool_use_block("goto", {"thought": "Navigating.", "url": "https://example.com"}))
    provider._client.messages.create = lambda **kwargs: response

    result = provider.decide("system prompt", "user prompt")

    assert result == {"action": "goto", "thought": "Navigating.", "args": {"url": "https://example.com"}}


def test_skips_a_leading_text_block_to_find_the_tool_use():
    # Real models sometimes emit a stray text block ("Let me think...")
    # before the actual tool call -- the loop should still find the tool.
    provider = _provider()
    response = _response(
        _text_block("Let me look at this page first."),
        _tool_use_block("finish", {"thought": "Done.", "summary": "The answer is 42."}),
    )
    provider._client.messages.create = lambda **kwargs: response

    result = provider.decide("system prompt", "user prompt")

    assert result["action"] == "finish"
    assert result["args"] == {"summary": "The answer is 42."}


def test_raises_llm_error_when_no_tool_use_block_present():
    provider = _provider()
    response = _response(_text_block("I don't want to call a tool."))
    provider._client.messages.create = lambda **kwargs: response

    with pytest.raises(LLMError, match="did not call a tool"):
        provider.decide("system prompt", "user prompt")


def test_wraps_api_errors_as_llm_error():
    provider = _provider()

    def boom(**kwargs):
        raise RuntimeError("connection reset")

    provider._client.messages.create = boom

    with pytest.raises(LLMError, match="Anthropic request failed"):
        provider.decide("system prompt", "user prompt")


def test_missing_thought_defaults_to_empty_string():
    provider = _provider()
    response = _response(_tool_use_block("scroll", {"direction": "down"}))
    provider._client.messages.create = lambda **kwargs: response

    result = provider.decide("system prompt", "user prompt")

    assert result["thought"] == ""
    assert result["args"] == {"direction": "down"}


def test_token_usage_accumulates_across_multiple_decide_calls():
    provider = _provider()
    provider._client.messages.create = lambda **kwargs: _response(
        _tool_use_block("wait", {}), input_tokens=100, output_tokens=20,
    )
    provider.decide("system prompt", "user prompt")
    provider._client.messages.create = lambda **kwargs: _response(
        _tool_use_block("wait", {}), input_tokens=150, output_tokens=30,
    )
    provider.decide("system prompt", "user prompt")

    assert provider.total_input_tokens == 250
    assert provider.total_output_tokens == 50


def test_token_usage_starts_at_zero_before_any_call():
    provider = _provider()
    assert provider.total_input_tokens == 0
    assert provider.total_output_tokens == 0


def test_max_retries_is_passed_through_to_the_sdk_client():
    provider = AnthropicProvider(api_key="test-key", model="claude-test", tool_specs=SOME_TOOL_SPECS, max_retries=5)
    assert provider._client.max_retries == 5


def test_max_retries_defaults_to_two():
    provider = _provider()
    assert provider._client.max_retries == 2
