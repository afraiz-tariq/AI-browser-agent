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


def test_max_retries_is_passed_through_to_the_sdk_client():
    provider = OpenAIProvider(api_key="test-key", model="gpt-test", tool_specs=SOME_TOOL_SPECS, max_retries=5)
    assert provider._client.max_retries == 5


def test_max_retries_defaults_to_two():
    provider = _provider()
    assert provider._client.max_retries == 2


# --- OpenAI-compatible providers: DeepSeek, Gemini, OpenRouter, local ----------

class _BadRequest(Exception):
    """Stands in for openai.BadRequestError: the SDK's errors carry status_code."""
    status_code = 400


def test_a_rejected_optional_setting_is_dropped_and_the_request_retried():
    # GPT-5 models accept only the default temperature; a 400 naming it must
    # not fail the task -- drop it, retry, and don't send it again.
    provider = _provider()
    sent = []

    def create(**kwargs):
        sent.append(dict(kwargs))
        if "temperature" in kwargs:
            raise _BadRequest("Error code: 400 - Unsupported value: 'temperature' does not support 0")
        return _response([_tool_call("finish", {"summary": "ok"})])

    provider._client.chat.completions.create = create
    assert provider.decide("s", "u")["action"] == "finish"
    provider.decide("s", "u")
    assert ["temperature" in k for k in sent] == [True, False, False]
    assert all(k["tool_choice"] == "required" for k in sent)  # only the rejected setting went


def test_other_errors_are_not_retried():
    provider = _provider()
    calls = []

    def create(**kwargs):
        calls.append(1)
        raise _BadRequest("Error code: 400 - Your credit balance is too low")

    provider._client.chat.completions.create = create
    with pytest.raises(LLMError):
        provider.decide("s", "u")
    assert len(calls) == 1


def test_deepseek_gets_thinking_turned_off_and_its_own_address():
    provider = OpenAIProvider("k", "deepseek-v4.1-flash", SOME_TOOL_SPECS, base_url="https://api.deepseek.com")
    sent = {}
    provider._client.chat.completions.create = lambda **kw: sent.update(kw) or _response(
        [_tool_call("finish", {"summary": "ok"})])
    provider.decide("s", "u")
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
    assert str(provider._client.base_url).startswith("https://api.deepseek.com")


def test_cached_prompt_tokens_are_counted_like_anthropics():
    # So the eval report compares costs fairly: input = uncached part.
    provider = _provider()
    openai_style = SimpleNamespace(prompt_tokens=6600, completion_tokens=100,
                                   prompt_tokens_details=SimpleNamespace(cached_tokens=5500))
    deepseek_style = SimpleNamespace(prompt_tokens=6600, completion_tokens=100, prompt_cache_hit_tokens=5000)
    for usage in (openai_style, deepseek_style):
        provider._client.chat.completions.create = lambda usage=usage, **kw: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[_tool_call("finish", {"summary": "x"})]))],
            usage=usage)
        provider.decide("s", "u")
    assert provider.total_input_tokens == 1100 + 1600
    assert provider.total_cache_read_tokens == 5500 + 5000


@pytest.mark.parametrize("provider_name, key_field, url_start", [
    ("deepseek", "deepseek_api_key", "https://api.deepseek.com"),
    ("gemini", "gemini_api_key", "https://generativelanguage.googleapis.com"),
    ("openrouter", "openrouter_api_key", "https://openrouter.ai"),
])
def test_provider_presets_pick_the_right_address_and_key(provider_name, key_field, url_start):
    from config import Config
    from llm import LLMClient

    config = Config(llm_provider=provider_name, llm_model="some-model", **{key_field: "the-key"})
    assert config.validate() == []
    client = LLMClient.from_config(config, SOME_TOOL_SPECS)
    assert str(client._provider._client.base_url).startswith(url_start)
    assert client._provider._client.api_key == "the-key"


def test_a_preset_without_its_key_is_a_config_problem():
    from config import Config

    assert any("DEEPSEEK_API_KEY" in p for p in Config(llm_provider="deepseek").validate())


def test_openai_base_url_overrides_the_preset_for_local_servers():
    from config import Config
    from llm import LLMClient

    config = Config(llm_provider="openai", llm_model="local", openai_api_key="x",
                    openai_base_url="http://localhost:1234/v1")
    client = LLMClient.from_config(config, SOME_TOOL_SPECS)
    assert str(client._provider._client.base_url).startswith("http://localhost:1234")


def test_errors_name_the_real_provider():
    provider = OpenAIProvider("k", "m", SOME_TOOL_SPECS, base_url="https://api.deepseek.com")

    def fail(**kwargs):
        raise RuntimeError("Error code: 402 - Insufficient Balance")

    provider._client.chat.completions.create = fail
    with pytest.raises(LLMError, match=r"OpenAI request failed \(api\.deepseek\.com\)"):
        provider.decide("s", "u")
