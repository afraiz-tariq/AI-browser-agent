"""
Excel "arm": structured spreadsheet actions via openpyxl.

Deliberately NOT mouse/UI automation on a running Excel window -- openpyxl
reads and writes .xlsx files directly and deterministically (a cell either
has the value you wrote or it doesn't), which is far more reliable than
clicking cells and hoping the right one was focused. This is why the
original Phase 2 design calls for "openpyxl for files, Excel COM/xlwings
only when interacting with a running Excel instance, UI automation only
when necessary" -- this module is the first (and most reliable) of those
three.

Scope cut, on purpose: this only handles files that are closed. It cannot
read from or write into an Excel window the user already has open (that
needs COM/xlwings, a different tool this same "arm" could grow into later
if a real task needs it). Trying to open a file that's actually open in
Excel right now will fail with a clear, expected error rather than
silently fighting Excel's file lock.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl

from tool_provider import ToolProvider, ToolSpec


class ExcelError(Exception):
    """Raised for any Excel arm failure (bad path, bad sheet, bad cell, ...)."""


def _coerce(value: str) -> Any:
    """Tool arguments arrive as strings; write plain numbers as numbers so
    formulas/SUM in the sheet still work, not as text that merely looks
    numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


# Registered into llm.py's flat tool list alongside the browser actions --
# see llm.py's ACTION_SPECS. Every action here is prefixed "excel_" so the
# agent loop's dispatcher (agent.py's _execute_action) can route by name
# without the two arms needing to know about each other otherwise.
EXCEL_ACTION_SPECS: dict[str, dict[str, Any]] = {
    "excel_open": {
        "description": "Open an existing .xlsx workbook, or create a new blank one if it doesn't exist yet. "
                        "Must be called before any other excel_* action.",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the .xlsx file."},
            "create_if_missing": {
                "type": "boolean",
                "description": "If true, create a new blank workbook when the file doesn't exist yet.",
            },
        },
        "required": ["path"],
        "risk_level": "R0",
    },
    "excel_list_sheets": {
        "description": "List the sheet names in the currently open workbook.",
        "properties": {},
        "required": [],
        "risk_level": "R0",
    },
    "excel_read_cell": {
        "description": "Read the value of a single cell.",
        "properties": {
            "sheet": {"type": "string", "description": "Sheet name."},
            "cell": {"type": "string", "description": "Cell reference, e.g. 'A1'."},
        },
        "required": ["sheet", "cell"],
        "risk_level": "R0",
    },
    "excel_read_range": {
        "description": "Read a rectangular range of cells as rows of values -- use this to scan a table "
                        "instead of reading cell-by-cell.",
        "properties": {
            "sheet": {"type": "string", "description": "Sheet name."},
            "cell_range": {"type": "string", "description": "Range reference, e.g. 'A1:C10'."},
        },
        "required": ["sheet", "cell_range"],
        "risk_level": "R0",
    },
    "excel_write_cell": {
        "description": "Write a value into a single cell. This does not save to disk -- call excel_save "
                        "when all edits for this task are done.",
        "properties": {
            "sheet": {"type": "string", "description": "Sheet name."},
            "cell": {"type": "string", "description": "Cell reference, e.g. 'B2'."},
            "value": {
                "type": "string",
                "description": "Value to write. Plain digits (e.g. '42' or '3.5') are written as real "
                                "numbers, not text, so formulas referencing this cell still work.",
            },
        },
        "required": ["sheet", "cell", "value"],
        # In-memory only, cheap to undo (just don't excel_save) -- reversible
        # write, R1. Confirms only if CONFIRM_R1_ACTIONS is explicitly
        # turned on in .env; off by default, matching original behavior
        # (excel_write_cell was never in the old ALWAYS_CONFIRM_EXCEL_ACTIONS set).
        "risk_level": "R1",
    },
    "excel_save": {
        "description": "Save the open workbook to disk, overwriting the file it was opened from unless a "
                        "different path is given. This is the only excel_ action that touches disk -- "
                        "reads and excel_write_cell only change the in-memory workbook.",
        "properties": {
            "path": {"type": "string", "description": "Optional: save to a different path instead of overwriting."},
        },
        "required": [],
        "risk_level": "R2",  # touches disk -- matches original ALWAYS_CONFIRM_EXCEL_ACTIONS behavior
    },
}


class ExcelSession:
    """Owns at most one open openpyxl Workbook for the lifetime of a task."""

    def __init__(self) -> None:
        self._workbook = None
        self._path: Path | None = None

    def is_open(self) -> bool:
        return self._workbook is not None

    @property
    def path(self) -> Path | None:
        """The path the open workbook was opened from / last saved to, if any."""
        return self._path

    def execute(self, action: str, args: dict) -> str:
        """
        Dispatch one excel_* action and return a short, human-readable
        result string. That string is what makes this arm work inside the
        existing agent loop without needing a browser-style "observe the
        whole environment every step": agent.py appends it straight into
        the action's own history entry, so the model sees exactly what it
        asked for (e.g. "Sheet1!A1 = 42") on its very next turn.
        """
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            raise ExcelError(f"Unknown Excel action: {action}")
        return handler(args)

    def _do_excel_open(self, args: dict) -> str:
        path = Path(args["path"]).expanduser()
        if path.exists():
            try:
                self._workbook = openpyxl.load_workbook(path)
            except Exception as e:
                raise ExcelError(
                    f"Could not open '{path}': {e}. It may be open in Excel right now (file locked), "
                    "or not be a valid .xlsx file."
                ) from e
            self._path = path
            return f"Opened '{path}'. Sheets: {', '.join(self._workbook.sheetnames)}"
        if args.get("create_if_missing"):
            self._workbook = openpyxl.Workbook()
            self._path = path
            return f"'{path}' did not exist -- created a new blank workbook (unsaved until excel_save)."
        raise ExcelError(f"File not found: {path}. Pass create_if_missing=true to create a new one.")

    def _require_open(self) -> None:
        if self._workbook is None:
            raise ExcelError("No workbook is open. Call excel_open first.")

    def _do_excel_list_sheets(self, args: dict) -> str:
        self._require_open()
        return "Sheets: " + ", ".join(self._workbook.sheetnames)

    def _sheet(self, name: str):
        self._require_open()
        if name not in self._workbook.sheetnames:
            raise ExcelError(f"No sheet named '{name}'. Available: {', '.join(self._workbook.sheetnames)}")
        return self._workbook[name]

    def _do_excel_read_cell(self, args: dict) -> str:
        ws = self._sheet(args["sheet"])
        value = ws[args["cell"]].value
        return f"{args['sheet']}!{args['cell']} = {value!r}"

    def _do_excel_read_range(self, args: dict) -> str:
        ws = self._sheet(args["sheet"])
        rows = [[cell.value for cell in row] for row in ws[args["cell_range"]]]
        return f"{args['sheet']}!{args['cell_range']} = {rows}"

    def _do_excel_write_cell(self, args: dict) -> str:
        ws = self._sheet(args["sheet"])
        value = _coerce(args["value"])
        ws[args["cell"]] = value
        return f"Wrote {value!r} to {args['sheet']}!{args['cell']} (not saved yet -- call excel_save)"

    def _do_excel_save(self, args: dict) -> str:
        self._require_open()
        target = Path(args["path"]).expanduser() if args.get("path") else self._path
        if target is None:
            raise ExcelError("No path to save to -- excel_open a file first, or pass path explicitly.")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._workbook.save(target)
        self._path = target
        return f"Saved workbook to '{target}'."

    def close(self) -> None:
        self._workbook = None
        self._path = None


class ExcelToolProvider(ToolProvider):
    """Wraps an ExcelSession to satisfy the ToolProvider contract. Owns no
    logic of its own beyond dispatch/description glue -- all the actual
    openpyxl mechanics stay in ExcelSession above, unchanged."""

    def __init__(self, excel_session: ExcelSession):
        self.excel_session = excel_session

    def get_tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=name, description=spec["description"], properties=spec["properties"],
                required=spec["required"], risk_level=spec["risk_level"],
            )
            for name, spec in EXCEL_ACTION_SPECS.items()
        ]

    def execute(self, name: str, args: dict) -> str | None:
        return self.excel_session.execute(name, args)

    def describe_for_confirmation(self, name: str, args: dict) -> str:
        if name == "excel_save":
            target = args.get("path") or self.excel_session.path or "(current file)"
            return f"save the workbook to '{target}', overwriting it"
        return super().describe_for_confirmation(name, args)

    # ensure_ready(), get_dynamic_risk(), verify(), wants_verification() all
    # use ToolProvider's defaults -- Excel has no process to lazily start,
    # every action's risk is fully determined by its static risk_level, and
    # Excel actions are deterministic/self-reporting so there's nothing for
    # VERIFY to check.
