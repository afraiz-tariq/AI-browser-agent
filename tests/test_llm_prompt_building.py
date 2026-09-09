"""
Tests for LLMClient.decide_next_action()'s prompt-building logic in
llm.py -- the actual TASK/ACTION HISTORY/OBSERVATION text sent to the
model. Every existing test drives this through MockProvider, which
records each (system, user) prompt pair in `.calls` specifically so it
can be asserted on -- but nothing ever actually read `.calls` before this
file. A bug here (a broken history truncation, a malformed element list)
would have silently changed what the model sees without any test noticing.
"""
from browser import ElementInfo, Observation
from llm import LLMClient, LLMError, MockProvider


def _observation(**overrides):
    defaults = dict(
        url="https://example.com", title="Example Page", elements=[], visible_text="Hello world",
        looks_like_login=False, state_fingerprint="", text_truncated=False, total_text_length=len("Hello world"),
    )
    defaults.update(overrides)
    return Observation(**defaults)


def test_no_observation_says_no_browser_page_is_open():
    mock = MockProvider([{"action": "wait", "args": {}}])
    client = LLMClient(mock)

    client.decide_next_action("Do something.", [], None)

    _, user_prompt = mock.calls[0]
    assert "OBSERVATION: No browser page is currently open." in user_prompt


def test_observation_is_rendered_with_url_title_and_elements():
    mock = MockProvider([{"action": "wait", "args": {}}])
    client = LLMClient(mock)
    obs = _observation(
        url="https://shop.example/cart", title="Your Cart", looks_like_login=True,
        elements=[
            ElementInfo(index=0, tag="button", role="button", text="Checkout", input_type=""),
            ElementInfo(index=1, tag="input", role="textbox", text="Promo code", input_type="text"),
        ],
        visible_text="Cart contents here",
    )

    client.decide_next_action("Buy the thing.", [], obs)

    _, user_prompt = mock.calls[0]
    assert "URL: https://shop.example/cart" in user_prompt
    assert "TITLE: Your Cart" in user_prompt
    assert "LOOKS LIKE LOGIN PAGE: True" in user_prompt
    assert "[0] <button> 'Checkout'" in user_prompt
    assert "[1] <input/text> 'Promo code'" in user_prompt
    assert "Cart contents here" in user_prompt


def test_truncated_text_tells_the_model_there_is_more_and_to_scroll():
    mock = MockProvider([{"action": "wait", "args": {}}])
    client = LLMClient(mock)
    obs = _observation(visible_text="a" * 100, text_truncated=True, total_text_length=5000)

    client.decide_next_action("Read the whole page.", [], obs)

    _, user_prompt = mock.calls[0]
    assert "showing 100 of 5000 characters" in user_prompt
    assert "scroll" in user_prompt.lower()


def test_non_truncated_text_does_not_falsely_claim_there_is_more():
    # Regression test: the label used to say "(truncated)" unconditionally,
    # even for a short page that was never actually cut off.
    mock = MockProvider([{"action": "wait", "args": {}}])
    client = LLMClient(mock)
    obs = _observation(visible_text="short page", text_truncated=False, total_text_length=10)

    client.decide_next_action("Read the page.", [], obs)

    _, user_prompt = mock.calls[0]
    assert "VISIBLE TEXT:" in user_prompt
    assert "more text than what's shown" not in user_prompt


def test_observation_with_no_interactive_elements_says_so():
    mock = MockProvider([{"action": "wait", "args": {}}])
    client = LLMClient(mock)

    client.decide_next_action("Read the page.", [], _observation(elements=[]))

    _, user_prompt = mock.calls[0]
    assert "(no interactive elements found)" in user_prompt


def test_empty_history_says_this_is_the_first_step():
    mock = MockProvider([{"action": "wait", "args": {}}])
    client = LLMClient(mock)

    client.decide_next_action("A task.", [], None)

    _, user_prompt = mock.calls[0]
    assert "(none yet, this is the first step)" in user_prompt


def test_history_is_truncated_to_the_last_eight_entries():
    mock = MockProvider([{"action": "wait", "args": {}}])
    client = LLMClient(mock)
    history = [f"step {i}" for i in range(1, 13)]  # 12 entries

    client.decide_next_action("A task.", history, None)

    _, user_prompt = mock.calls[0]
    for i in range(5, 13):  # last 8 of 12 -- steps 5 through 12
        assert f"step {i}" in user_prompt
    for i in range(1, 5):  # the 4 oldest, dropped
        assert f"- step {i}\n" not in user_prompt


def test_task_text_is_included_verbatim():
    mock = MockProvider([{"action": "wait", "args": {}}])
    client = LLMClient(mock)

    client.decide_next_action("Find the capital of France.", [], None)

    _, user_prompt = mock.calls[0]
    assert "TASK: Find the capital of France." in user_prompt


def test_missing_action_key_raises_llm_error():
    mock = MockProvider([{"thought": "no action field here", "args": {}}])
    client = LLMClient(mock)

    try:
        client.decide_next_action("A task.", [], None)
        assert False, "expected an LLMError"
    except LLMError as e:
        assert "action" in str(e).lower()


def test_missing_args_key_defaults_to_empty_dict():
    mock = MockProvider([{"action": "extract", "thought": "no args field here"}])
    client = LLMClient(mock)

    result = client.decide_next_action("A task.", [], None)

    assert result["args"] == {}
