"""
Windows desktop automation "arm": launch apps and drive their controls via
UI Automation (pywinauto, backend="uia") -- the same reliability class as
Playwright reading the DOM (a real accessibility tree), NOT blind pixel/
coordinate clicking (e.g. pyautogui).

Deliberately narrow scope, per ARCHITECTURE_DECISIONS.md section 5's
recorded decision ("Windows desktop automation (general): far more
open-ended/brittle than browser (DOM) or Excel (file format); scope down to
launch-app + known-dialogs only"). Two things follow from that:

1. Confirmation policy mirrors the browser arm's, not a new pattern:
   windows_click_control's risk is DYNAMIC (WindowsToolProvider.
   get_dynamic_risk()), the same mechanism as BrowserToolProvider.
   get_dynamic_risk() checking is_sensitive() -- windows_list_controls
   already reads each control's real accessible text via UI Automation,
   the same kind of ground truth the DOM gives the browser arm, so a click
   only confirms (R2) when the target control's own text matches a
   sensitive-keyword list (SENSITIVE_KEYWORDS below, browser.py's list
   extended with Windows-relevant destructive actions); otherwise R0, no
   confirmation. windows_type_into_control is R1 (confirms only if
   CONFIRM_R1_ACTIONS is on) -- typing itself is reversible, the risk lives
   in whatever button gets pressed afterward, same reasoning as the browser
   arm's `type` action without submit=True. windows_launch_app is R2
   (confirms by default, tunable off). windows_close_window stays R2 --
   there's no control-text signal to judge a whole-window close by, so it
   keeps a default-yes confirm. See WINDOWS_ACTION_SPECS and
   WindowsToolProvider.get_dynamic_risk() below.
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
- _connect_window's title matching originally went straight to a substring
  regex (".*<title>.*"), raising an error only when 2+ windows matched.
  Real usage (a model typing a just-typed word as a guessed window_title on
  a busy desktop with many windows open) showed the failure mode that
  design missed: exactly ONE window matched the guessed substring, but it
  was the WRONG window -- no error, just silent action against an unrelated
  window's controls. Fixed by trying an EXACT title match first (immune to
  this, since a real title can't accidentally collide the way a short
  guessed fragment can), falling back to the substring match only when no
  exact match exists; see _connect_window().
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from browser import SENSITIVE_KEYWORDS as BROWSER_SENSITIVE_KEYWORDS
from errors import TaskCannotBeCompleted, explain
from secret_fields import HIDDEN
from tool_provider import RiskLevel, ToolProvider, ToolSpec


class WindowsAutomationError(Exception):
    """Raised for any Windows arm failure (window/control not found, ...)."""


def _is_password_control(ctrl) -> bool:
    """
    UI Automation's own IsPassword flag -- the Windows equivalent of an
    <input type="password">. A control's window_text() goes straight to the
    model (windows_list_controls, windows_read_control_text), so a password
    box's contents must never be read through it; see secret_fields.py.
    Windows usually masks these itself, but this doesn't rely on that.

    Only a real bool/int True counts: any failure to read the flag (older
    control, win32 backend, a mock in tests) means "not flagged", falling
    back to the app's own masking rather than blocking every control.
    """
    try:
        flag = ctrl.element_info.element.CurrentIsPassword
    except Exception:
        return False
    return type(flag) in (bool, int) and bool(flag)


def resolve_known_folders() -> dict[str, str]:
    """
    Resolve this account's real Desktop/Documents locations, including
    OneDrive-redirected ones -- so the model can be told the true path up
    front instead of guessing.

    Found from a real bot run: asked to save a file "on desktop", the model
    had no way to know the actual path and tried 'C:\\Users\\Public\\Desktop'
    (PermissionError), 'C:\\Users\\User\\Desktop' and a bare guessed
    '<username>\\Desktop' (both WinError 5) across many steps before finally
    landing on the real OneDrive-redirected Desktop -- see excel_tools.py's
    module docstring for why the Excel arm itself can't paper over this (it
    only ever does exactly what path it's given). Reading the same registry
    values Explorer uses (User Shell Folders) is what makes redirection
    visible; a plain os.path.expanduser("~/Desktop") guess would still be
    wrong on this kind of machine.

    Falls back to plain expanduser-based guesses if winreg isn't available
    (e.g. non-Windows, where this whole arm is inactive anyway).
    """
    home = os.path.expanduser("~")
    folders = {"Home": home, "Desktop": os.path.join(home, "Desktop"), "Documents": os.path.join(home, "Documents")}
    try:
        import winreg  # Windows-only stdlib module -- lazy import, same reasoning as pywinauto above
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            for label, value_name in (("Desktop", "Desktop"), ("Documents", "Personal")):
                try:
                    raw, _ = winreg.QueryValueEx(key, value_name)
                    resolved = os.path.expandvars(raw)
                    # A stale registry entry (moved/unlinked OneDrive, migrated
                    # profile) would otherwise hand the LLM a confident-looking
                    # but non-existent path -- recreating the exact bug this
                    # function exists to avoid, just one level removed.
                    if os.path.isdir(resolved):
                        folders[label] = resolved
                except FileNotFoundError:
                    pass
    except (OSError, ImportError):
        # ImportError (specifically ModuleNotFoundError, which is NOT an
        # OSError subclass) is what `import winreg` raises on non-Windows --
        # without catching it too, this crashed run_task() on every platform
        # other than Windows, regardless of whether the Windows arm is enabled.
        pass
    return folders


# pywinauto's type_keys() interprets these as keystroke-modifier syntax
# (e.g. "(" starts a key-combo group) unless escaped by wrapping in braces
# -- without this, literal text containing any of them would be silently
# mangled. See _do_windows_type_into_control().
_TYPE_KEYS_SPECIAL_CHARS = set("+^%~(){}[]")

# set_focus() returns as soon as the focus CHANGE is requested, not once
# the control has actually finished receiving it -- real-window testing
# found type_keys() firing immediately after set_focus() dropped the first
# character(s) typed ("hello world" -> "hello orld") on a fresh window, and
# produced worse garbling/repeats ("hello world" -> "hello ddddd") on a
# busier one. Both traced to this timing race and type_keys()'s default
# keystroke rate, not the escaping logic -- see _do_windows_type_into_control().
_FOCUS_SETTLE_DELAY_S = 0.15
_TYPE_KEYS_PAUSE_S = 0.03


def _escape_for_type_keys(text: str) -> str:
    return "".join(f"{{{c}}}" if c in _TYPE_KEYS_SPECIAL_CHARS else c for c in text)


# Extends browser.py's SENSITIVE_KEYWORDS (submit/buy/delete/...) with
# Windows-relevant destructive-sounding actions that wouldn't naturally
# appear in a web page's button text -- used by
# WindowsToolProvider.get_dynamic_risk() the same way browser.py's
# is_sensitive() uses its own list.
WINDOWS_EXTRA_SENSITIVE_KEYWORDS = (
    "uninstall", "format", "erase", "reset", "wipe", "shut down", "restart", "sign out",
)
SENSITIVE_KEYWORDS = BROWSER_SENSITIVE_KEYWORDS + WINDOWS_EXTRA_SENSITIVE_KEYWORDS


# Registered into llm.py's flat tool list alongside the other arms' specs --
# see WindowsToolProvider.get_tool_specs(). risk_level here is the STATIC/
# base tier; windows_click_control starts at R0 and gets escalated at call
# time by get_dynamic_risk() below (mirrors browser.py's BROWSER_ACTION_SPECS
# comment for the exact same reason: its real risk depends on the target
# control's own text, not the tool name alone). See module docstring.
WINDOWS_ACTION_SPECS: dict[str, dict[str, Any]] = {
    "windows_launch_app": {
        "description": "Launch a Windows application. Use this (or windows_list_windows to find an "
                        "already-running one) before any other windows_* action targets a window.",
        "properties": {
            "path": {"type": "string", "description": "Path to the executable, e.g. 'notepad.exe' or a full path."},
            "args": {"type": "string", "description": "Optional command-line arguments."},
        },
        "required": ["path"],
        "risk_level": "R2",
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
            "window_title": {
                "type": "string",
                "description": "The window's EXACT title, copied verbatim from windows_list_windows (or from "
                                "a window you just launched -- check windows_list_windows to see its real "
                                "title). Do not guess a short or generic substring (e.g. a word from text you "
                                "just typed) -- on a desktop with many windows open, a short substring can "
                                "silently match a completely unrelated window instead of raising an error.",
            },
        },
        "required": ["window_title"],
        "risk_level": "R0",
    },
    "windows_click_control": {
        "description": "Click a control by index from the most recent windows_list_controls call for this window.",
        "properties": {
            "window_title": {
                "type": "string",
                "description": "The exact window_title used in the most recent windows_list_controls call "
                                "for this window -- see that tool's description for why it must be exact.",
            },
            "index": {"type": "integer", "description": "Control index from the most recent windows_list_controls."},
        },
        "required": ["window_title", "index"],
        "risk_level": "R0",
    },
    "windows_type_into_control": {
        "description": "Type text into a control by index from the most recent windows_list_controls call.",
        "properties": {
            "window_title": {
                "type": "string",
                "description": "The exact window_title used in the most recent windows_list_controls call "
                                "for this window -- see that tool's description for why it must be exact.",
            },
            "index": {"type": "integer", "description": "Control index from the most recent windows_list_controls."},
            "text": {"type": "string", "description": "Text to type into the control."},
        },
        "required": ["window_title", "index", "text"],
        "risk_level": "R1",
    },
    "windows_read_control_text": {
        "description": "Read the current text/value of a control by index from the most recent "
                        "windows_list_controls call. If a click/type action happened since that listing, "
                        "call windows_list_controls again first -- see its description for why.",
        "properties": {
            "window_title": {
                "type": "string",
                "description": "The exact window_title used in the most recent windows_list_controls call "
                                "for this window -- see that tool's description for why it must be exact.",
            },
            "index": {"type": "integer", "description": "Control index from the most recent windows_list_controls."},
        },
        "required": ["window_title", "index"],
        "risk_level": "R0",
    },
    "windows_close_window": {
        "description": "Close a window by title.",
        "properties": {
            "window_title": {
                "type": "string",
                "description": "The window's EXACT title, copied verbatim from windows_list_windows -- see "
                                "windows_list_controls's description for why a guessed substring is risky.",
            },
        },
        "required": ["window_title"],
        "risk_level": "R2",
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
        # The most recent windows_list_controls result as data, for the
        # optional Jev decider (jev.py, DECIDER=hybrid) to choose a control
        # from: (window_title, [{"i", "type", "text", "password"}, ...]).
        # The same lines the model already got as text -- no extra UIA
        # calls. Cleared when a launch or a close makes it about the wrong
        # window, so Jev never picks from a listing that no longer applies.
        self.last_listing: tuple[str, list[dict]] | None = None

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
        self.last_listing = None  # a new app is about to be in front; the old listing is for another window
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

        # Exact match first: if window_title is a real title returned by
        # windows_list_windows/windows_list_controls (as it should be), this
        # can't accidentally hit an unrelated window. Real-world testing
        # (see CHANGELOG.md) found the substring fallback below silently
        # connecting to a completely unrelated window that happened to be
        # the ONE match for a short, guessed substring (e.g. "Hello" instead
        # of the real "Hello from the agent - Notepad") -- no ambiguity
        # error, just a wrong window acted on as if it were the right one.
        # Substring matching only kicks in when there's no exact match, for
        # a caller that genuinely doesn't know the full title yet.
        try:
            app = Application(backend="uia").connect(title=window_title)
            return app.window(title=window_title)
        except Exception:
            pass

        pattern = f".*{re.escape(window_title)}.*"
        try:
            app = Application(backend="uia").connect(title_re=pattern)
            return app.window(title_re=pattern)
        except Exception as e:
            raise WindowsAutomationError(
                f"Could not find an open window matching '{window_title}': {e}. "
                "Use windows_list_windows to see currently open window titles, and pass one back "
                "EXACTLY -- a short guessed substring can silently match an unrelated window."
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
        listing = []
        for i, ctrl in enumerate(controls):
            try:
                ctrl_type = ctrl.friendly_class_name()
                if _is_password_control(ctrl):
                    lines.append(f"[{i}] {ctrl_type} (password field) '{HIDDEN}'")
                    listing.append({"i": i, "type": ctrl_type, "text": HIDDEN, "password": True})
                    continue
                text = " ".join((ctrl.window_text() or "").split())[:80]
            except Exception:
                ctrl_type, text = "unknown", ""
            lines.append(f"[{i}] {ctrl_type} '{text}'")
            listing.append({"i": i, "type": ctrl_type, "text": text, "password": False})
        self.last_listing = (window_title, listing)
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

    def get_control_text(self, window_title: str, index: int) -> str:
        """
        Best-effort accessible text of a control, for risk classification
        (see WindowsToolProvider.get_dynamic_risk()) -- returns "" rather
        than raising if the control can't be resolved (not listed yet, bad
        index, or the UIA call itself fails), since a risk check that can't
        determine the text should fall through to the static risk tier, not
        blow up the whole dispatch. Actual execution (_do_windows_click_control)
        still uses _resolve_control(), which DOES raise a clear error on a
        bad index -- this method is deliberately more forgiving, matching
        browser.py's is_sensitive()/element_summary() same graceful-
        degradation shape.
        """
        controls = self._last_controls.get(window_title)
        if not controls or not (0 <= index < len(controls)):
            return ""
        try:
            return controls[index].window_text() or ""
        except Exception:
            return ""

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
            # See _FOCUS_SETTLE_DELAY_S/_TYPE_KEYS_PAUSE_S above -- without
            # both of these, real-window testing found dropped/garbled
            # characters even with the escaping already correct.
            time.sleep(_FOCUS_SETTLE_DELAY_S)
            ctrl.type_keys(_escape_for_type_keys(text), with_spaces=True, pause=_TYPE_KEYS_PAUSE_S)
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
        if _is_password_control(ctrl):
            return f"Control #{index} in '{window_title}' is a password field; its contents are never read."
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
        if self.last_listing and self.last_listing[0] == window_title:
            self.last_listing = None
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

    def get_dynamic_risk(self, name: str, args: dict) -> RiskLevel | None:
        # Mirrors BrowserToolProvider.get_dynamic_risk() exactly: only
        # windows_click_control's risk depends on its target (the resolved
        # control's own accessible text, the UIA-backed equivalent of a
        # button's visible label) -- windows_type_into_control and
        # windows_launch_app/windows_close_window get their risk entirely
        # from their static risk_level in WINDOWS_ACTION_SPECS, so this
        # returns None for them (falls through to that static tier).
        if name == "windows_click_control":
            window_title = args.get("window_title")
            index = args.get("index")
            if window_title is not None and index is not None:
                text = self.session.get_control_text(window_title, int(index)).lower()
                if any(keyword in text for keyword in SENSITIVE_KEYWORDS):
                    return "R2"
        return None

    # verify() and wants_verification() use ToolProvider's defaults --
    # there's no "observe the whole window every step" equivalent for
    # VERIFY to check against; each action already reports its own result
    # directly, same as the Excel arm.
