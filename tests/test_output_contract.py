"""
Tests for the structured result record run_task() writes to output/ on
every run (ARCHITECTURE_DECISIONS.md section 4's "structured JSON result
contracts") -- a small evolution of the old bare {task, result, saved_at}
file: now written for failures too, and includes what the task actually
touched (artifacts) and any VERIFY warnings raised along the way.
"""
import json

from agent import run_task
from llm import ZERO_USAGE, LLMClient, MockProvider


def _reply(thought, action, args):
    return json.dumps({"thought": thought, "action": action, "args": args})


def _read_output(path: str) -> dict:
    return json.loads(open(path, encoding="utf-8").read())


def test_successful_task_writes_a_structured_record_with_artifacts(test_config, tmp_path):
    xlsx_path = tmp_path / "data.xlsx"
    mock = MockProvider([
        _reply("Opening.", "excel_open", {"path": str(xlsx_path), "create_if_missing": True}),
        _reply("Writing.", "excel_write_cell", {"sheet": "Sheet", "cell": "A1", "value": "42"}),
        _reply("Saving.", "excel_save", {}),
        _reply("Done.", "finish", {"summary": "Wrote 42 to A1 and saved the workbook."}),
    ])
    outcome = run_task("Write 42 to A1 and save.", test_config, dry_run=True, llm_client=LLMClient(mock))

    assert outcome["success"] is True
    record = _read_output(outcome["output_path"])
    assert record["status"] == "success"
    assert record["summary"] == "Wrote 42 to A1 and saved the workbook."
    assert record["steps_taken"] == 4
    assert {"type": "excel_file_opened", "path": str(xlsx_path)} in record["artifacts"]
    assert {"type": "excel_file_saved", "path": str(xlsx_path)} in record["artifacts"]
    assert record["verification_warnings"] == []
    assert "saved_at" in record
    # MockProvider never touches token counters -- all zeros is the honest
    # answer for a scripted run, not a missing one (see llm.py's
    # BaseLLMProvider and LLMClient.get_usage()).
    assert record["token_usage"] == ZERO_USAGE


def test_failed_task_also_writes_a_structured_record(test_config, fixtures_server, monkeypatch):
    # Regression: previously _save_output() was only called on success, so
    # a failed task left no trace at all under output/ -- only in logs/.
    monkeypatch.setattr("agent.ask_confirmation", lambda prompt: False)
    mock = MockProvider([
        _reply("Navigating to the account settings page.", "goto", {"url": f"{fixtures_server}/sensitive_button.html"}),
        _reply("Deleting the account.", "click", {"index": 0}),
    ])

    outcome = run_task("Delete the account.", test_config, dry_run=False, llm_client=LLMClient(mock))

    assert outcome["success"] is False
    record = _read_output(outcome["output_path"])
    assert record["status"] == "failed"
    assert "declined" in record["summary"].lower()
    assert record["steps_taken"] >= 1
    # The goto that ran before the declined click still shows up as a
    # touched artifact, even though the task ultimately failed.
    assert {"type": "url_visited", "url": f"{fixtures_server}/sensitive_button.html"} in record["artifacts"]
    assert record["token_usage"] == ZERO_USAGE


def test_record_includes_per_step_timings_without_confirmation_wait(test_config, fixtures_server, monkeypatch):
    # Phase 0 baseline (docs/JEV_VOICE_PLAN.md): every step's observe/decide/
    # act time is recorded, and a human's time answering [y/n] is kept out
    # of act_ms -- otherwise a slow "y" would read as a slow agent.
    import time as _time

    def slow_yes(prompt):
        _time.sleep(0.3)
        return True

    mock = MockProvider([
        _reply("Opening the page.", "goto", {"url": f"{fixtures_server}/sensitive_button.html"}),
        _reply("Clicking delete.", "click", {"index": 0}),
        _reply("Done.", "finish", {"summary": "Clicked the delete button on the fixture page."}),
    ])
    outcome = run_task(
        "Click delete.", test_config, dry_run=False, llm_client=LLMClient(mock), confirm_callback=slow_yes,
    )

    assert outcome["success"] is True
    timings = _read_output(outcome["output_path"])["timings"]
    steps = timings["steps"]
    assert [s["action"] for s in steps] == ["goto", "click", "finish"]
    assert all("decide_ms" in s for s in steps)
    assert "observe_ms" not in steps[0]  # no page open before the first goto
    assert "observe_ms" in steps[1]
    click = steps[1]
    assert click["confirm_wait_ms"] >= 300
    assert click["act_ms"] < click["confirm_wait_ms"]
    assert timings["median_decide_ms"] is not None
    assert timings["median_observe_ms"] is not None


def test_timings_summary_is_empty_but_well_formed_when_no_step_ran():
    from agent import summarize_timings

    assert summarize_timings([]) == {
        "median_observe_ms": None, "median_decide_ms": None, "median_act_ms": None, "steps": [],
    }
