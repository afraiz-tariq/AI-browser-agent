"""
Integration tests for the Excel arm running through the full agent loop
(agent.py's run_task), proving the orchestrator design actually works:
- an Excel-only task never touches the browser at all (lazy Chrome launch)
- a single task can mix browser and Excel actions freely
- excel_save gets the same [y/n] confirmation gate as a sensitive browser click
"""
import json
from unittest.mock import MagicMock

import openpyxl

from agent import run_task
from llm import LLMClient, MockProvider


def _reply(thought, action, args):
    return json.dumps({"thought": thought, "action": action, "args": args, "confidence": "high"})


def test_excel_only_task_never_launches_browser(test_config, tmp_path, monkeypatch):
    xlsx_path = tmp_path / "data.xlsx"
    start_spy = MagicMock()
    monkeypatch.setattr("browser.BrowserSession.start", start_spy)

    mock = MockProvider([
        _reply("Creating a new workbook.", "excel_open", {"path": str(xlsx_path), "create_if_missing": True}),
        _reply("Writing a value.", "excel_write_cell", {"sheet": "Sheet", "cell": "A1", "value": "42"}),
        _reply("Saving.", "excel_save", {}),
        _reply("Done.", "finish", {"summary": "Wrote 42 to A1 and saved the workbook."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Create a spreadsheet and write 42 to A1.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is True
    assert xlsx_path.exists()
    start_spy.assert_not_called()

    wb = openpyxl.load_workbook(xlsx_path)
    assert wb["Sheet"]["A1"].value == 42


def test_mixed_browser_and_excel_task(test_config, fixtures_server, tmp_path):
    xlsx_path = tmp_path / "data.xlsx"
    mock = MockProvider([
        _reply("Opening the spreadsheet first.", "excel_open", {"path": str(xlsx_path), "create_if_missing": True}),
        _reply("Recording a placeholder before checking the web.", "excel_write_cell",
               {"sheet": "Sheet", "cell": "A1", "value": "OpenAI"}),
        _reply("Now checking the mock search engine too.", "goto", {"url": f"{fixtures_server}/index.html"}),
        _reply("Saving the workbook.", "excel_save", {}),
        _reply("Done.", "finish", {"summary": "Visited the mock search engine and wrote 'OpenAI' to the spreadsheet."}),
    ])
    llm_client = LLMClient(mock)

    outcome = run_task("Do a mixed browser+Excel task.", test_config, dry_run=True, llm_client=llm_client)

    assert outcome["success"] is True
    wb = openpyxl.load_workbook(xlsx_path)
    assert wb["Sheet"]["A1"].value == "OpenAI"


def test_declined_excel_save_stops_the_task_and_nothing_is_written(test_config, tmp_path, monkeypatch):
    xlsx_path = tmp_path / "data.xlsx"
    monkeypatch.setattr("agent.ask_confirmation", lambda prompt: False)

    mock = MockProvider([
        _reply("Creating a new workbook.", "excel_open", {"path": str(xlsx_path), "create_if_missing": True}),
        _reply("Saving.", "excel_save", {}),
    ])
    llm_client = LLMClient(mock)

    # dry_run=False on purpose: dry_run skips confirmation prompts entirely,
    # which would defeat this test.
    outcome = run_task("Create and save a workbook.", test_config, dry_run=False, llm_client=llm_client)

    assert outcome["success"] is False
    assert "declined" in outcome["result"].lower()
    assert not xlsx_path.exists()
