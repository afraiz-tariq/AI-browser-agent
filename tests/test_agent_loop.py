"""
End-to-end test of the full observe -> think -> act loop, using a
MockProvider instead of a real LLM. This is what `python agent.py
--dry-run` is for in practice: it proves the wiring between browser.py,
llm.py, and agent.py works without spending API credits or needing
internet access to a real search engine.
"""
import json

from agent import run_task
from llm import LLMClient, MockProvider


def _reply(thought, action, args, confidence="high"):
    return json.dumps({"thought": thought, "action": action, "args": args, "confidence": confidence})


def test_full_task_completes_with_mock_llm(test_config, fixtures_server, monkeypatch):
    # Script the exact 3 steps a real model would plausibly take on the
    # fixture "search engine": type + submit, extract, then finish.
    mock = MockProvider([
        _reply("I see a search box, I'll search for OpenAI.", "goto", {"url": f"{fixtures_server}/index.html"}),
        _reply("Typing the query and submitting the form.", "type", {"index": 0, "text": "OpenAI", "submit": True}),
        _reply("The results page has what I need.", "extract", {}),
        _reply("I have enough information to answer.", "finish",
               {"summary": "OpenAI is an AI research and deployment company that builds models like GPT and DALL-E."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Open the mock search engine and search for OpenAI.", test_config,
                        dry_run=True, llm_client=llm_client)

    assert outcome["success"] is True
    assert "OpenAI is an AI research" in outcome["result"]
    assert outcome["output_path"]


def test_declined_sensitive_action_stops_the_task(test_config, fixtures_server, monkeypatch):
    # Regression test: TaskCannotBeCompleted raised inside _execute_action
    # (a declined [y/n] confirmation) must stop the task, not be swallowed
    # by the broader "action failed, try again" except-and-continue clause.
    monkeypatch.setattr("agent.ask_confirmation", lambda prompt: False)
    mock = MockProvider([
        _reply("Navigating to the account settings page.", "goto", {"url": f"{fixtures_server}/sensitive_button.html"}),
        _reply("Deleting the account.", "click", {"index": 0}),
    ])
    llm_client = LLMClient(mock)

    # dry_run=False here on purpose: dry_run is what normally skips
    # confirmation prompts entirely, which would defeat this test.
    outcome = run_task("Delete the account.", test_config, dry_run=False, llm_client=llm_client)

    assert outcome["success"] is False
    assert "declined" in outcome["result"].lower()


def test_verify_step_flags_an_action_with_no_observable_effect(test_config, fixtures_server, capsys):
    # The VERIFY step should notice when an action that's supposed to
    # change the page (here, a click) leaves the URL and visible text both
    # completely unchanged, and say so -- without needing another LLM call
    # to eventually notice.
    mock = MockProvider([
        _reply("Navigating to the inert button page.", "goto", {"url": f"{fixtures_server}/inert_button.html"}),
        _reply("Clicking the button.", "click", {"index": 0}),
        _reply("Done.", "finish", {"summary": "The inert button page was visited."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Click the button.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is True
    captured = capsys.readouterr()
    assert "[verify]" in captured.out
    assert "no observable change" in captured.out


def test_login_wall_stops_task_cleanly(test_config, fixtures_server):
    mock = MockProvider([
        _reply("Navigating to the login-protected page.", "goto", {"url": f"{fixtures_server}/login_wall.html"}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Log in and check my account.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is False
    assert "login" in outcome["result"].lower() or "manual" in outcome["result"].lower()


def test_empty_finish_summary_is_rejected_and_retried(test_config, fixtures_server):
    # Regression test: a model that calls finish without actually writing
    # an answer (args={} or {"summary": ""}) must be asked to try again
    # instead of the task silently succeeding with "(no summary provided)".
    mock = MockProvider([
        _reply("Navigating.", "goto", {"url": f"{fixtures_server}/index.html"}),
        _reply("Search results are showing, task complete.", "finish", {}),
        _reply("Here is the actual answer.", "finish", {"summary": "The mock search engine loaded successfully."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Open the mock search engine.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is True
    assert outcome["result"] == "The mock search engine loaded successfully."


def test_three_empty_finishes_gives_up_with_specific_error(test_config, fixtures_server):
    # If the model just won't write a summary even after being asked to
    # retry twice, the task should fail with a dedicated explanation --
    # and NOT be swallowed by the generic "repeated action" stuck-guard,
    # which would otherwise fire on three identical finish({}) calls too.
    empty_finish = _reply("Task complete.", "finish", {})
    mock = MockProvider([
        _reply("Navigating.", "goto", {"url": f"{fixtures_server}/index.html"}),
        empty_finish,
        empty_finish,
        empty_finish,
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Open the mock search engine.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is False
    assert "without ever writing a summary" in outcome["result"]


def test_repeated_action_is_detected_as_stuck(test_config, fixtures_server):
    same_reply = _reply("Scrolling to see more.", "scroll", {"direction": "down"})
    mock = MockProvider([
        _reply("Navigating.", "goto", {"url": f"{fixtures_server}/index.html"}),
        same_reply,
        same_reply,
        same_reply,
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Scroll forever.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is False
    assert "repeated the same action" in outcome["result"]
