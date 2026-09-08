"""
Tests for the structured result record run_task() writes to output/ on
every run (ARCHITECTURE_DECISIONS.md section 4's "structured JSON result
contracts") -- a small evolution of the old bare {task, result, saved_at}
file: now written for failures too, and includes what the task actually
touched (artifacts) and any VERIFY warnings raised along the way.
"""
import json

from agent import run_task
from llm import LLMClient, MockProvider


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
