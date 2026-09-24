"""
Tests for evals/run_evals.py's own scoring mechanics (run_single_eval) --
NOT for the eval tasks' real-world correctness, which by design can only
be judged by running evals/run_evals.py against a real LLM (see its module
docstring). These tests inject a MockProvider so the harness itself --
building the task text, calling run_task(), applying check(), reading
token_usage back out of the saved output record, honoring `requires()`
skips and teardown() -- is proven correct without needing a real API key,
the same "verify the mechanism, not the model" split as the rest of tests/.
"""
import json

from llm import ZERO_USAGE, LLMClient, MockProvider

from evals.run_evals import run_single_eval
from evals.tasks import TASK_EXCEL_WRITE_ROUNDTRIP, TASK_MCP_FETCH, EvalTask


def _reply(thought, action, args):
    return json.dumps({"thought": thought, "action": action, "args": args})


def test_passing_task_reports_passed_true_with_token_usage(test_config, tmp_path):
    # Must match TASK_EXCEL_WRITE_ROUNDTRIP.build()'s own filename -- the
    # MockProvider's scripted reply stands in for a real model choosing to
    # open exactly the path the task text asked for.
    xlsx_path = tmp_path / "eval_write_roundtrip.xlsx"
    mock = MockProvider([
        _reply("Creating.", "excel_open", {"path": str(xlsx_path), "create_if_missing": True}),
        _reply("Writing.", "excel_write_cell", {"sheet": "Sheet", "cell": "A1", "value": "42"}),
        _reply("Saving.", "excel_save", {}),
        _reply("Done.", "finish", {"summary": "Wrote 42 to A1 and saved."}),
    ])

    result = run_single_eval(
        TASK_EXCEL_WRITE_ROUNDTRIP, test_config, tmp_path, fixtures_server=None, llm_client=LLMClient(mock),
    )

    assert result.passed is True
    assert "correct value" in result.detail.lower()
    # MockProvider never touches token counters -- all zeros is the honest
    # answer for a scripted run, read back from the saved output record.
    assert result.token_usage == ZERO_USAGE


def test_failing_task_reports_passed_false_with_detail(test_config, tmp_path):
    # The model never writes anything -- finishes immediately -- so the
    # workbook never gets created and the checker should catch that.
    mock = MockProvider([_reply("Never mind.", "finish", {"summary": "Done."})])

    result = run_single_eval(
        TASK_EXCEL_WRITE_ROUNDTRIP, test_config, tmp_path, fixtures_server=None, llm_client=LLMClient(mock),
    )

    assert result.passed is False
    assert "never created" in result.detail.lower()


def test_task_skipped_cleanly_when_requires_is_false(test_config, tmp_path):
    # test_config never turns ENABLE_MCP_FETCH on, so this task's
    # requires(config) is False -- it should be skipped, not attempted
    # (and definitely not fail from a missing MCP server).
    result = run_single_eval(TASK_MCP_FETCH, test_config, tmp_path, fixtures_server=None, llm_client=LLMClient(MockProvider([])))

    assert result.passed is None
    assert "skipped" in result.detail.lower()


def test_teardown_runs_even_when_check_raises(test_config, tmp_path):
    teardown_calls = []
    task = EvalTask(
        id="harness_teardown_check",
        description="Verifies teardown still runs if check() raises.",
        build=lambda ctx: "irrelevant",
        check=lambda outcome, ctx: (_ for _ in ()).throw(RuntimeError("boom")),
        teardown=lambda ctx: teardown_calls.append(ctx),
    )
    mock = MockProvider([_reply("Done.", "finish", {"summary": "Done."})])

    try:
        run_single_eval(task, test_config, tmp_path, fixtures_server=None, llm_client=LLMClient(mock))
    except RuntimeError:
        pass

    assert len(teardown_calls) == 1


def test_timings_are_read_back_and_summarized(test_config, tmp_path):
    # The Phase 0 baseline (docs/JEV_VOICE_PLAN.md) comes from these numbers,
    # so the harness must carry run_task()'s per-step timings through and
    # save_results() must write them with no secrets or page text.
    from evals.run_evals import save_results, speed_summary

    xlsx_path = tmp_path / "eval_write_roundtrip.xlsx"
    mock = MockProvider([
        _reply("Creating.", "excel_open", {"path": str(xlsx_path), "create_if_missing": True}),
        _reply("Writing.", "excel_write_cell", {"sheet": "Sheet", "cell": "A1", "value": "42"}),
        _reply("Saving.", "excel_save", {}),
        _reply("Done.", "finish", {"summary": "Wrote 42 to A1 and saved."}),
    ])
    result = run_single_eval(
        TASK_EXCEL_WRITE_ROUNDTRIP, test_config, tmp_path, fixtures_server=None, llm_client=LLMClient(mock),
    )

    assert [s["action"] for s in result.timings["steps"]] == ["excel_open", "excel_write_cell", "excel_save", "finish"]
    summary = speed_summary([result])
    assert summary["tasks_ran"] == 1 and summary["tasks_passed"] == 1
    assert summary["median_steps_per_task"] == 4
    assert summary["median_decide_ms"] is not None
    assert summary["median_observe_ms"] is None  # Excel-only: no page was ever observed

    out = tmp_path / "baseline.json"
    save_results([result], test_config, out)
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["summary"] == summary
    assert saved["tasks"][0]["task_id"] == TASK_EXCEL_WRITE_ROUNDTRIP.id


def _run_click_task(task, replies, test_config, tmp_path, fixtures_server):
    """Returns (result, the prompt the model saw before its final step) --
    the latter proves what the page really showed after the scripted clicks."""
    mock = MockProvider(replies)
    result = run_single_eval(task, test_config, tmp_path, fixtures_server=fixtures_server, llm_client=LLMClient(mock))
    return result, mock.calls[-1][1]


def test_settings_toggles_fixture_scores_the_real_checkbox_states(test_config, tmp_path, fixtures_server):
    # The page's code comes from the checkboxes' real states when Apply is
    # pressed, so the check can't be passed by the summary alone.
    from evals.tasks import TASK_SETTINGS_TOGGLES

    url = f"{fixtures_server}/settings_toggles.html"
    right, page = _run_click_task(TASK_SETTINGS_TOGGLES, [
        _reply("Open.", "goto", {"url": url}),
        _reply("Dark on.", "click", {"index": 0}),
        _reply("Email off.", "click", {"index": 1}),
        _reply("Compact on.", "click", {"index": 2}),
        _reply("Apply.", "click", {"index": 4}),
        _reply("Done.", "finish", {"summary": "The page says: Confirmation code: D1-E0-C1-S1"}),
    ], test_config, tmp_path, fixtures_server)
    assert right.passed is True
    assert "Confirmation code: D1-E0-C1-S1" in page

    wrong, page = _run_click_task(TASK_SETTINGS_TOGGLES, [
        _reply("Open.", "goto", {"url": url}),
        _reply("Apply.", "click", {"index": 4}),
        _reply("Done.", "finish", {"summary": "The page says: Confirmation code: D0-E1-C0-S1"}),
    ], test_config, tmp_path, fixtures_server)
    assert wrong.passed is False
    assert "Confirmation code: D0-E1-C0-S1" in page


def test_trip_wizard_fixture_walks_three_steps(test_config, tmp_path, fixtures_server):
    from evals.tasks import TASK_TRIP_WIZARD

    result, page = _run_click_task(TASK_TRIP_WIZARD, [
        _reply("Open.", "goto", {"url": f"{fixtures_server}/trip_wizard.html"}),
        _reply("Lisbon.", "click", {"index": 0}),
        _reply("Next.", "click", {"index": 3}),
        _reply("3 nights.", "click", {"index": 1}),  # step 2 is now the only visible section
        _reply("Next.", "click", {"index": 3}),
        _reply("Done.", "finish", {"summary": "Your trip: Lisbon, 3 nights. Reference: TRIP-LIS-3"}),
    ], test_config, tmp_path, fixtures_server)
    assert result.passed is True
    assert "Reference: TRIP-LIS-3" in page


def test_an_account_problem_stops_the_eval_run():
    # An unfunded DeepSeek account made all 11 tasks "fail" in a second each;
    # the runner should stop at the first one and say why.
    from evals.run_evals import EvalResult, account_problem

    no_credit = EvalResult("t", "d", False, "Task reported failure: WHAT HAPPENED: Your AI provider account has "
                                            "run out of credit.\nWHY: 402 Insufficient Balance", 0.6, {})
    assert "run out of credit" in account_problem(no_credit)
    ordinary = EvalResult("t", "d", False, "Expected 'San Francisco' in the summary", 5.0, {})
    assert account_problem(ordinary) is None
    assert account_problem(EvalResult("t", "d", None, "Skipped", 0.0, {})) is None


def test_a_wrong_model_name_also_stops_the_eval_run():
    from evals.run_evals import EvalResult, account_problem

    wrong = EvalResult("t", "d", False, "Task reported failure: WHAT HAPPENED: Your AI provider doesn't know the "
                                        "model name in LLM_MODEL.", 0.4, {})
    assert "LLM_MODEL" in account_problem(wrong)
