"""
Tests for OpenAIProvider's own decide() logic in llm.py -- see
test_llm_providers.py's module docstring for the Anthropic equivalent and
the general approach (a real client, only the network-calling method
faked). Split into its own file because `openai` is an optional dependency
(requirements.txt -- only needed for LLM_PROVIDER=openai): skipped
entirely if it isn't installed, same pattern as test_mcp_tools.py's `mcp`
skip, without dragging the always-required Anthropic tests down with it.
"""
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("openai")

from llm import LLMError, OpenAIProvider  # noqa: E402
from tool_provider import ToolSpec  # noqa: E402

SOME_TOOL_SPECS = [ToolSpec(name="finish", description="Finish.", properties={}, required=[], risk_level="R0")]


def _provider():
    return OpenAIProvider(api_key="test-key", model="gpt-test", tool_specs=SOME_TOOL_SPECS)


def _response(tool_calls, prompt_tokens=10, completion_tokens=5):
    message = SimpleNamespace(tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def _tool_call(name, arguments_dict_or_str):
    args = arguments_dict_or_str if isinstance(arguments_dict_or_str, str) else json.dumps(arguments_dict_or_str)
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))


def test_parses_a_tool_call():
    provider = _provider()
    response = _response([_tool_call("click", {"thought": "Clicking submit.", "index": 3})])
    provider._client.chat.completions.create = lambda **kwargs: response

    result = provider.decide("system prompt", "user prompt")

    assert result == {"action": "click", "thought": "Clicking submit.", "args": {"index": 3}}


def test_raises_llm_error_when_no_tool_calls():
    provider = _provider()
    response = _response([])
    provider._client.chat.completions.create = lambda **kwargs: response

    with pytest.raises(LLMError, match="did not call a tool"):
        provider.decide("system prompt", "user prompt")


def test_raises_llm_error_when_tool_calls_is_none():
    provider = _provider()
    response = _response(None)
    provider._client.chat.completions.create = lambda **kwargs: response

    with pytest.raises(LLMError, match="did not call a tool"):
        provider.decide("system prompt", "user prompt")


def test_raises_llm_error_on_invalid_json_arguments():
    provider = _provider()
    response = _response([_tool_call("goto", "{not valid json")])
    provider._client.chat.completions.create = lambda **kwargs: response

    with pytest.raises(LLMError, match="invalid tool arguments"):
        provider.decide("system prompt", "user prompt")


def test_treats_empty_arguments_as_empty_object():
    provider = _provider()
    response = _response([_tool_call("extract", "")])
    provider._client.chat.completions.create = lambda **kwargs: response

    result = provider.decide("system prompt", "user prompt")

    assert result == {"action": "extract", "thought": "", "args": {}}


def test_wraps_api_errors_as_llm_error():
    provider = _provider()

    def boom(**kwargs):
        raise RuntimeError("rate limited")

    provider._client.chat.completions.create = boom

    with pytest.raises(LLMError, match="OpenAI request failed"):
        provider.decide("system prompt", "user prompt")


def test_token_usage_accumulates_across_multiple_decide_calls():
    provider = _provider()
    provider._client.chat.completions.create = lambda **kwargs: _response(
        [_tool_call("wait", {})], prompt_tokens=100, completion_tokens=20,
    )
    provider.decide("system prompt", "user prompt")
    provider._client.chat.completions.create = lambda **kwargs: _response(
        [_tool_call("wait", {})], prompt_tokens=150, completion_tokens=30,
    )
    provider.decide("system prompt", "user prompt")

    assert provider.total_input_tokens == 250
    assert provider.total_output_tokens == 50


def test_token_usage_starts_at_zero_before_any_call():
    provider = _provider()
    assert provider.total_input_tokens == 0
    assert provider.total_output_tokens == 0
