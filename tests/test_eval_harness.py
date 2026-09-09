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

from llm import LLMClient, MockProvider

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
    # MockProvider never touches token counters -- {0, 0} is the honest
    # answer for a scripted run, read back from the saved output record.
    assert result.token_usage == {"input_tokens": 0, "output_tokens": 0}


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
