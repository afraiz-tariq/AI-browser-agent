"""
End-to-end test of the full observe -> think -> act loop, using a
MockProvider instead of a real LLM. This is what `python agent.py
--dry-run` is for in practice: it proves the wiring between browser.py,
llm.py, and agent.py works without spending API credits or needing
internet access to a real search engine.
"""
import json
from unittest.mock import MagicMock

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


def test_custom_confirm_callback_is_used_and_can_approve(test_config, fixtures_server):
    # Regression test for the Discord bot (and any other non-terminal
    # front-end): run_task() must use an injected confirm_callback instead
    # of the default ask_confirmation()/input() when one is provided --
    # that's what lets a bot ask y/n in a chat instead of blocking on a
    # terminal that isn't attached.
    confirm = MagicMock(return_value=True)
    mock = MockProvider([
        _reply("Navigating to the account settings page.", "goto", {"url": f"{fixtures_server}/sensitive_button.html"}),
        _reply("Deleting the account.", "click", {"index": 0}),
        _reply("Done.", "finish", {"summary": "The account was deleted."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task(
        "Delete the account.", test_config, dry_run=False, llm_client=llm_client, confirm_callback=confirm,
    )

    assert confirm.called
    assert outcome["success"] is True


def test_custom_confirm_callback_can_decline(test_config, fixtures_server):
    confirm = MagicMock(return_value=False)
    mock = MockProvider([
        _reply("Navigating to the account settings page.", "goto", {"url": f"{fixtures_server}/sensitive_button.html"}),
        _reply("Deleting the account.", "click", {"index": 0}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task(
        "Delete the account.", test_config, dry_run=False, llm_client=llm_client, confirm_callback=confirm,
    )

    assert confirm.called
    assert outcome["success"] is False
    assert "declined" in outcome["result"].lower()


def test_verify_step_does_not_false_flag_a_checkbox_click(test_config, fixtures_server, capsys):
    # Regression test for a real failure: VERIFY used to compare only URL
    # and visible text, so a successful checkbox click (which changes
    # neither) looked identical to a failed one -- convincing the model
    # its own working click had failed, and sending it into a repeated-
    # clicking spiral until it tripped the stuck-loop guard. Comparing
    # element state too (see browser.py's state_fingerprint) fixes that.
    mock = MockProvider([
        _reply("Navigating to the checkbox page.", "goto", {"url": f"{fixtures_server}/checkbox_page.html"}),
        _reply("Checking the checkbox.", "click", {"index": 0}),
        _reply("Done.", "finish", {"summary": "The checkbox was checked."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Check the checkbox.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is True
    captured = capsys.readouterr()
    assert "[verify]" not in captured.out


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


def test_oscillating_between_two_different_actions_is_detected_as_stuck(test_config, fixtures_server):
    # No single action repeats three times in a row here (the exact-repeat
    # guard alone wouldn't catch this), but the model never makes any real
    # progress either -- just bounces between two actions indefinitely.
    mock = MockProvider([
        _reply("Navigating.", "goto", {"url": f"{fixtures_server}/index.html"}),
        _reply("Scrolling down.", "scroll", {"direction": "down"}),
        _reply("Scrolling up.", "scroll", {"direction": "up"}),
        _reply("Scrolling down again.", "scroll", {"direction": "down"}),
        _reply("Scrolling up again.", "scroll", {"direction": "up"}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Oscillate forever.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is False
    assert "oscillating between two actions" in outcome["result"]


def test_repeated_no_effect_actions_get_a_hint_then_eventually_abort(test_config, fixtures_server, capsys):
    # Different elements each time (not a literal repeat, and not an A-B-A-B
    # oscillation either), but every single click has zero observable
    # effect -- the exact-repeat and oscillation guards would both miss
    # this; only the consecutive-no-effect counter catches it.
    mock = MockProvider([
        _reply("Navigating to several inert buttons.", "goto", {"url": f"{fixtures_server}/several_inert_buttons.html"}),
        _reply("Clicking the first button.", "click", {"index": 0}),
        _reply("Clicking the second button.", "click", {"index": 1}),
        _reply("Clicking the third button.", "click", {"index": 2}),
        _reply("Clicking the fourth button.", "click", {"index": 3}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Click every button.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is False
    assert "no observable progress" in outcome["result"]
    captured = capsys.readouterr()
    assert "[hint]" in captured.out  # the earlier self-correction nudge fired first, before the hard abort


def test_a_single_no_effect_action_does_not_trigger_the_progress_guard(test_config, fixtures_server):
    # One VERIFY warning alone must not be treated as "stuck" -- only a
    # run of several in a row. Regression guard against an overly
    # trigger-happy threshold.
    mock = MockProvider([
        _reply("Navigating.", "goto", {"url": f"{fixtures_server}/several_inert_buttons.html"}),
        _reply("Clicking a button that does nothing.", "click", {"index": 0}),
        _reply("Done.", "finish", {"summary": "Clicked the first button; it had no visible effect."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Click a button.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is True


def test_progress_between_no_effect_actions_resets_the_counter(test_config, fixtures_server, tmp_path):
    # A real action succeeding in between two no-effect clicks (here, an
    # Excel write -- something outside what VERIFY even tracks) must reset
    # the streak, not let it silently accumulate toward the abort threshold.
    xlsx_path = tmp_path / "data.xlsx"
    mock = MockProvider([
        _reply("Navigating.", "goto", {"url": f"{fixtures_server}/several_inert_buttons.html"}),
        _reply("Clicking a button that does nothing.", "click", {"index": 0}),
        _reply("Also opening a spreadsheet.", "excel_open", {"path": str(xlsx_path), "create_if_missing": True}),
        _reply("Clicking another button that does nothing.", "click", {"index": 1}),
        _reply("Done.", "finish", {"summary": "Explored the page and the spreadsheet."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Explore a bit.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is True


def test_scrolling_lets_the_model_read_past_the_initial_truncation(test_config, fixtures_server):
    # End-to-end proof that browser.py's text-pagination fix (scroll now
    # actually advances what observe() shows, instead of being a no-op for
    # text extraction) is wired all the way through the real loop. Checks
    # the actual prompts sent at each step (MockProvider.calls), not just
    # that the scripted run completes -- a scripted "finish" can't by
    # itself prove scrolling revealed anything real.
    mock = MockProvider([
        _reply("Navigating to the long page.", "goto", {"url": f"{fixtures_server}/long_page.html"}),
        _reply("Text is truncated -- scrolling for more.", "scroll", {"direction": "down"}),
        _reply("Still truncated -- scrolling again.", "scroll", {"direction": "down"}),
        _reply("Found it.", "finish", {"summary": "The page's last segment is SEGMENT-23."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Find the last segment marker on the long page.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is True
    prompts = [user_prompt for _, user_prompt in mock.calls]
    # prompts[0] is the very first decide call, made before "goto" has run
    # (no browser page open yet); prompts[1] is the first real observation.
    assert "SEGMENT-23" not in prompts[1]  # not visible before any scrolling
    assert "SEGMENT-23" in prompts[-1]  # revealed after scrolling down twice
    assert "more text than what's shown" in prompts[1].lower()  # told about the truncation up front
