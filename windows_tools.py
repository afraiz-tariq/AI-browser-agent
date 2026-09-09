"""
Windows desktop automation "arm": launch apps and drive their controls via
UI Automation (pywinauto, backend="uia") -- the same reliability class as
Playwright reading the DOM (a real accessibility tree), NOT blind pixel/
coordinate clicking (e.g. pyautogui).

Deliberately narrow scope, per ARCHITECTURE_DECISIONS.md section 5's
recorded decision ("Windows desktop automation (general): far more
open-ended/brittle than browser (DOM) or Excel (file format); scope down to
launch-app + known-dialogs only ... with default-confirm on every action,
not opt-in like the browser arm"). Two things follow from that directly:

1. Every mutating action is R3 (always confirms -- see tool_provider.py's
   requires_confirmation(), which makes R3 non-configurable). Only reads
   (windows_list_windows, windows_list_controls, windows_read_control_text)
   are R0. Unlike the browser arm, there's no dynamic per-argument risk
   tiering here (no windows-arm equivalent of "this click target's text
   looks like a submit button") -- there's no DOM-equivalent ground truth
   to justify treating any specific control as lower-risk.
2. Controls are addressed by index from the most recent windows_list_controls
   call for that window, never a name/selector the model guesses -- mirrors
   browser.py's observe() -> click(index)/type(index, ...) pattern exactly.

pywinauto is imported lazily, inside methods, never at module import time,
so the rest of the codebase and test suite keep working on a machine
without pywinauto installed (or on non-Windows) when this arm is off
(ENABLE_WINDOWS_AUTOMATION=false, the default -- see config.py).

Two things found only by testing against real windows (Notepad, Calculator
-- see CHANGELOG.md), not from pywinauto's docs alone:
- Typing text via UIA's ValuePattern (pywinauto's set_edit_text) silently
  wrote CORRUPTED text (no exception, just wrong content) into a modern
  WinUI-based app's text control. type_keys() (real simulated keystrokes,
  what every app receives identically) is the primary method instead; see
  _do_windows_type_into_control() and _escape_for_type_keys().
- A control reference from windows_list_controls can go stale the moment
  the app updates that control's content in place (observed on Calculator's
  result display after clicking '='): windows_read_control_text on the old
  reference returned the PRE-click value. windows_list_controls must be
  called again after an action, before reading a control affected by it --
  this is documented in the tool descriptions below for the model, mirroring
  browser.py's observe-after-act pattern.
- click_control originally used click_input() (real synthetic mouse input
  at the control's on-screen coordinates) -- but that requires the target
  window to be focused/foreground/unobscured, and a full end-to-end run
  through the actual agent loop (LLM-driven, real time passing between
  steps) found it silently doing nothing to Calculator whenever something
  else had focus between steps, with no exception raised. That's exactly
  the "blind pixel/coordinate clicking" this arm is meant to avoid (see
  above). invoke() (UIA's InvokePattern, activating the control directly
  through the accessibility API, no real mouse or focus needed) is the
  primary method instead; see _do_windows_click_control().
"""
from __future__ import annotations

import re
from typing import Any

from errors import TaskCannotBeCompleted, explain
from tool_provider import ToolProvider, ToolSpec


class WindowsAutomationError(Exception):
    """Raised for any Windows arm failure (window/control not found, ...)."""


# pywinauto's type_keys() interprets these as keystroke-modifier syntax
# (e.g. "(" starts a key-combo group) unless escaped by wrapping in braces
# -- without this, literal text containing any of them would be silently
# mangled. See _do_windows_type_into_control().
_TYPE_KEYS_SPECIAL_CHARS = set("+^%~(){}[]")


def _escape_for_type_keys(text: str) -> str:
    return "".join(f"{{{c}}}" if c in _TYPE_KEYS_SPECIAL_CHARS else c for c in text)


# Registered into llm.py's flat tool list alongside the other arms' specs --
# see WindowsToolProvider.get_tool_specs(). Every mutating action is R3 on
# purpose (see module docstring); only the three pure reads are R0.
WINDOWS_ACTION_SPECS: dict[str, dict[str, Any]] = {
    "windows_launch_app": {
        "description": "Launch a Windows application. Use this (or windows_list_windows to find an "
                        "already-running one) before any other windows_* action targets a window.",
        "properties": {
            "path": {"type": "string", "description": "Path to the executable, e.g. 'notepad.exe' or a full path."},
            "args": {"type": "string", "description": "Optional command-line arguments."},
        },
        "required": ["path"],
        "risk_level": "R3",
    },
    "windows_list_windows": {
        "description": "List the titles of currently open top-level windows.",
        "properties": {},
        "required": [],
        "risk_level": "R0",
    },
    "windows_list_controls": {
        "description": "List the indexed controls (buttons, text fields, ...) inside one window, by title. "
                        "Call this before windows_click_control / windows_type_into_control / "
                        "windows_read_control_text -- their 'index' argument refers to the list this returns, "
                        "and only to the MOST RECENT call for that exact window_title. Also call this again "
                        "AFTER any click/type action before reading a control's text -- some apps replace a "
                        "control's underlying element when its content changes (e.g. a calculator's result "
                        "display), so a reference from before the action can report stale, pre-action text.",
        "properties": {
            "window_title": {"type": "string", "description": "Window title, or a substring of it."},
        },
        "required": ["window_title"],
        "risk_level": "R0",
    },
    "windows_click_control": {
        "description": "Click a control by index from the most recent windows_list_controls call for this window.",
        "properties": {
            "window_title": {"type": "string", "description": "Same window_title used in windows_list_controls."},
            "index": {"type": "integer", "description": "Control index from the most recent windows_list_controls."},
        },
        "required": ["window_title", "index"],
        "risk_level": "R3",
    },
    "windows_type_into_control": {
        "description": "Type text into a control by index from the most recent windows_list_controls call.",
        "properties": {
            "window_title": {"type": "string", "description": "Same window_title used in windows_list_controls."},
            "index": {"type": "integer", "description": "Control index from the most recent windows_list_controls."},
            "text": {"type": "string", "description": "Text to type into the control."},
        },
        "required": ["window_title", "index", "text"],
        "risk_level": "R3",
    },
    "windows_read_control_text": {
        "description": "Read the current text/value of a control by index from the most recent "
                        "windows_list_controls call. If a click/type action happened since that listing, "
                        "call windows_list_controls again first -- see its description for why.",
        "properties": {
            "window_title": {"type": "string", "description": "Same window_title used in windows_list_controls."},
            "index": {"type": "integer", "description": "Control index from the most recent windows_list_controls."},
        },
        "required": ["window_title", "index"],
        "risk_level": "R0",
    },
    "windows_close_window": {
        "description": "Close a window by title.",
        "properties": {
            "window_title": {"type": "string", "description": "Window title (or substring) to close."},
        },
        "required": ["window_title"],
        "risk_level": "R3",
    },
}


class WindowsSession:
    """
    Owns pywinauto connection/control-listing state for one agent run. No
    subprocess or background thread of its own to manage -- pywinauto talks
    to Windows' UI Automation directly -- so there's deliberately no
    close()/cleanup method here, and agent.py's run_task() doesn't call one:
    an app the model launched should be left running when the task ends,
    not force-closed, since that could destroy the user's unsaved work in
    it (see agent.py's provider-construction comment).
    """

    def __init__(self) -> None:
        # window_title -> the exact list of pywinauto control wrappers most
        # recently returned for it, indexed exactly like the text listing
        # windows_list_controls() returned -- mirrors browser.py's
        # BrowserSession._last_elements, one list per window instead of one
        # global list since a task may have more than one window open.
        self._last_controls: dict[str, list] = {}

    def execute(self, action: str, args: dict) -> str:
        """
        Dispatch one windows_* action and return a short, human-readable
        result string -- same shape as ExcelSession.execute(): there's no
        browser-style "observe the whole environment every step" for a
        desktop window, so agent.py appends this straight into the action's
        own history entry.
        """
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            raise WindowsAutomationError(f"Unknown Windows action: {action}")
        return handler(args)

    def _do_windows_launch_app(self, args: dict) -> str:
        from pywinauto.application import Application

        path = args["path"]
        extra_args = args.get("args") or ""
        cmd_line = f'"{path}" {extra_args}'.strip()
        try:
            Application(backend="uia").start(cmd_line)
        except Exception as e:
            raise WindowsAutomationError(
                f"Could not launch '{path}': {e}. Check the path is correct and the file exists."
            ) from e
        return f"Launched '{path}'."

    def _do_windows_list_windows(self, args: dict) -> str:
        from pywinauto import Desktop

        try:
            windows = Desktop(backend="uia").windows()
        except Exception as e:
            raise WindowsAutomationError(f"Could not list open windows: {e}") from e
        titles = sorted({w.window_text() for w in windows if w.window_text()})
        if not titles:
            return "No open windows with a title were found."
        return "Open windows: " + "; ".join(titles)

    def _connect_window(self, window_title: str):
        from pywinauto.application import Application

        pattern = f".*{re.escape(window_title)}.*"
        try:
            app = Application(backend="uia").connect(title_re=pattern)
            return app.window(title_re=pattern)
        except Exception as e:
            raise WindowsAutomationError(
                f"Could not find an open window matching '{window_title}': {e}. "
                "Use windows_list_windows to see currently open window titles."
            ) from e

    def _do_windows_list_controls(self, args: dict) -> str:
        window_title = args["window_title"]
        window = self._connect_window(window_title)
        try:
            controls = window.descendants()
        except Exception as e:
            raise WindowsAutomationError(f"Could not list controls in '{window_title}': {e}") from e
        self._last_controls[window_title] = controls
        if not controls:
            return f"No controls found in '{window_title}'."
        lines = []
        for i, ctrl in enumerate(controls):
            try:
                ctrl_type = ctrl.friendly_class_name()
                text = " ".join((ctrl.window_text() or "").split())[:80]
            except Exception:
                ctrl_type, text = "unknown", ""
            lines.append(f"[{i}] {ctrl_type} '{text}'")
        return f"Controls in '{window_title}':\n" + "\n".join(lines)

    def _resolve_control(self, window_title: str, index: int):
        controls = self._last_controls.get(window_title)
        if not controls:
            raise WindowsAutomationError(
                f"No controls have been listed for '{window_title}' yet. Call windows_list_controls first."
            )
        if not (0 <= index < len(controls)):
            raise IndexError(
                f"Control #{index} does not exist in the last windows_list_controls('{window_title}') "
                f"(only {len(controls)} controls were seen)."
            )
        return controls[index]

    def _do_windows_click_control(self, args: dict) -> str:
        window_title = args["window_title"]
        index = int(args["index"])
        ctrl = self._resolve_control(window_title, index)
        try:
            # invoke() (UIA's InvokePattern) activates the control directly
            # through the accessibility API -- no real mouse movement, and
            # crucially no requirement that the window be focused/foreground/
            # unobscured. click_input() (real synthetic mouse input at the
            # control's on-screen coordinates) is the fallback, not the
            # primary: end-to-end testing through the full agent loop found
            # click_input() clicks silently landing wrong (a multi-step
            # button sequence produced no change at all) whenever something
            # else had focus between steps -- exactly the "blind pixel/
            # coordinate clicking" this arm is meant to avoid (see module
            # docstring). invoke() doesn't have that failure mode.
            ctrl.invoke()
        except Exception:
            try:
                ctrl.click_input()
            except Exception as e:
                raise WindowsAutomationError(f"Could not click control #{index} in '{window_title}': {e}") from e
        return f"Clicked control #{index} in '{window_title}'."

    def _do_windows_type_into_control(self, args: dict) -> str:
        window_title = args["window_title"]
        index = int(args["index"])
        text = str(args.get("text", ""))
        ctrl = self._resolve_control(window_title, index)
        try:
            # type_keys() simulates real keyboard input (SendInput), which
            # every app receives identically regardless of UI framework --
            # this is the primary method, NOT set_edit_text (UIA's
            # ValuePattern.SetValue): real-window testing against a modern
            # WinUI-based app found set_edit_text silently writing corrupted
            # text (no exception raised, just wrong content) on a control
            # that reported supporting it, which type_keys does not do.
            # Escaping is required: type_keys interprets +^%~(){}[] as
            # keystroke-modifier syntax unless escaped, and would otherwise
            # silently mangle literal text containing them.
            ctrl.set_focus()
            ctrl.type_keys(_escape_for_type_keys(text), with_spaces=True)
        except Exception:
            # Fallback for a control that can't receive simulated keystrokes
            # (e.g. genuinely not focusable) but does support UIA's Value
            # pattern directly.
            ctrl.set_edit_text(text)
        return f"Typed {text!r} into control #{index} in '{window_title}'."

    def _do_windows_read_control_text(self, args: dict) -> str:
        window_title = args["window_title"]
        index = int(args["index"])
        ctrl = self._resolve_control(window_title, index)
        try:
            text = ctrl.window_text()
        except Exception as e:
            raise WindowsAutomationError(f"Could not read control #{index} in '{window_title}': {e}") from e
        return f"Control #{index} in '{window_title}' text: {text!r}"

    def _do_windows_close_window(self, args: dict) -> str:
        window_title = args["window_title"]
        window = self._connect_window(window_title)
        try:
            window.close()
        except Exception as e:
            raise WindowsAutomationError(f"Could not close '{window_title}': {e}") from e
        self._last_controls.pop(window_title, None)
        return f"Closed '{window_title}'."


class WindowsToolProvider(ToolProvider):
    """Wraps a WindowsSession to satisfy the ToolProvider contract. Owns no
    logic of its own beyond dispatch/description glue -- all the actual
    pywinauto mechanics stay in WindowsSession above, unchanged."""

    def __init__(self, session: WindowsSession):
        self.session = session

    def get_tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=name, description=spec["description"], properties=spec["properties"],
                required=spec["required"], risk_level=spec["risk_level"],
            )
            for name, spec in WINDOWS_ACTION_SPECS.items()
        ]

    def ensure_ready(self) -> None:
        # A defensive, friendly check on top of config.py's own validate()
        # (which refuses to even start a task with ENABLE_WINDOWS_AUTOMATION
        # on when sys.platform != "win32"): validate() can't catch "the
        # package just isn't pip-installed yet" ahead of time the way it
        # catches the OS mismatch, so this is where that surfaces instead.
        try:
            import pywinauto  # noqa: F401
        except ImportError as e:
            raise TaskCannotBeCompleted(
                explain(
                    "Windows desktop automation is enabled but pywinauto is not installed.",
                    "ENABLE_WINDOWS_AUTOMATION=true requires the pywinauto package.",
                    "Run 'pip install pywinauto' (Windows only), then try again.",
                )
            ) from e

    def describe_for_confirmation(self, name: str, args: dict) -> str:
        if name == "windows_launch_app":
            return f"launch '{args.get('path')}'"
        if name == "windows_click_control":
            return f"click control #{args.get('index')} in '{args.get('window_title')}'"
        if name == "windows_type_into_control":
            return f"type into control #{args.get('index')} in '{args.get('window_title')}'"
        if name == "windows_close_window":
            return f"close window '{args.get('window_title')}'"
        return super().describe_for_confirmation(name, args)

    def execute(self, name: str, args: dict) -> str | None:
        return self.session.execute(name, args)

    # get_dynamic_risk(), verify(), wants_verification() all use
    # ToolProvider's defaults -- every action's risk is fully determined by
    # its static risk_level (see module docstring point 1), and there's no
    # "observe the whole window every step" equivalent for VERIFY to check
    # against; each action already reports its own result directly, same as
    # the Excel arm.
