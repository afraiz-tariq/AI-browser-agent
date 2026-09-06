"""
Unit tests for the Excel arm (excel_tools.py), fully offline -- openpyxl
reads/writes real .xlsx files on disk, no browser or LLM involved.
"""
import openpyxl
import pytest

from excel_tools import ExcelError, ExcelSession


def _make_workbook(path, sheet_data):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for row in sheet_data:
        ws.append(row)
    wb.save(path)


def test_open_read_write_save_roundtrip(tmp_path):
    path = tmp_path / "data.xlsx"
    _make_workbook(path, [["Name", "Price"], ["Widget", 10], ["Gadget", 20]])

    session = ExcelSession()
    assert session.is_open() is False

    result = session.execute("excel_open", {"path": str(path)})
    assert "Sheet1" in result
    assert session.is_open() is True

    result = session.execute("excel_read_cell", {"sheet": "Sheet1", "cell": "A2"})
    assert "Widget" in result

    result = session.execute("excel_read_range", {"sheet": "Sheet1", "cell_range": "A1:B3"})
    assert "Widget" in result and "Gadget" in result

    session.execute("excel_write_cell", {"sheet": "Sheet1", "cell": "B2", "value": "15"})
    result = session.execute("excel_read_cell", {"sheet": "Sheet1", "cell": "B2"})
    assert "15" in result

    session.execute("excel_save", {})

    # Re-open fresh to prove the write actually landed on disk, not just in memory.
    reopened = openpyxl.load_workbook(path)
    assert reopened["Sheet1"]["B2"].value == 15
    assert isinstance(reopened["Sheet1"]["B2"].value, int)  # numeric coercion, not the string "15"


def test_create_if_missing(tmp_path):
    path = tmp_path / "new.xlsx"
    session = ExcelSession()

    with pytest.raises(ExcelError):
        session.execute("excel_open", {"path": str(path)})

    result = session.execute("excel_open", {"path": str(path), "create_if_missing": True})
    assert "did not exist" in result
    session.execute("excel_save", {})
    assert path.exists()


def test_unknown_sheet_and_missing_workbook_raise_clear_errors(tmp_path):
    path = tmp_path / "data.xlsx"
    _make_workbook(path, [["A"]])
    session = ExcelSession()

    with pytest.raises(ExcelError, match="No workbook is open"):
        session.execute("excel_read_cell", {"sheet": "Sheet1", "cell": "A1"})

    session.execute("excel_open", {"path": str(path)})
    with pytest.raises(ExcelError, match="No sheet named"):
        session.execute("excel_read_cell", {"sheet": "DoesNotExist", "cell": "A1"})


def test_list_sheets(tmp_path):
    path = tmp_path / "multi.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "First"
    wb.create_sheet("Second")
    wb.save(path)

    session = ExcelSession()
    session.execute("excel_open", {"path": str(path)})
    result = session.execute("excel_list_sheets", {})
    assert "First" in result and "Second" in result
