"""
The agent's main app window (Windows): a chat-style feed of each task -- what
you asked, every step as it happens, approval cards, the agent's questions,
the result -- with Pause / Take over / Stop, a history of past tasks, a
message box that works mid-task, and a compact always-on-top mode.

Asked for on 2026-09-25 after a look at how other agents present themselves
(Magentic-UI's co-tasking and action guards, ChatGPT agent's take-over and
narration, UI-TARS Desktop's timeline and history -- see
docs/JEV_VOICE_PLAN.md). The page is HTML/CSS/JS in ui/, shown by pywebview
with the Edge WebView2 engine Windows 10/11 already has; voice.py wires it to
the same VoiceAssistant the small tkinter window (voice_ui.py) uses, which
stays as the fallback when pywebview isn't installed.

Safety, unchanged from the rest of the project:
- Approvals go through the same confirm_callback; the buttons exist only
  while a question is open and run_task() is blocked on it. Answers are tied
  to that question's id, so a stale click can't answer a later one.
- Messages typed mid-task and answers to the agent's questions go into the
  TASK text (agent.py), never into the history where page/app text lives.
- The Windows arm refuses to list or touch these windows
  (windows_tools.OWN_WINDOW_TITLES), so the agent can't type into its own
  message box.
- Everything the page shows is inserted as text, never as HTML (ui/app.js
  has no innerHTML): page and app text the agent read can't become markup
  or script in a window that can answer approvals.

AppState, route_submit and the history readers are plain logic, tested
offline (tests/test_app_ui.py); the page itself was checked in Chromium with
a stand-in for the pywebview bridge.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from voice_ui import Choice, NullUi

APP_TITLE = "AI Agent"
COMPACT_TITLE = "AI Agent (compact)"
UI_DIR = Path(__file__).resolve().parent / "ui"
FEED_LIMIT = 400  # events kept for a window that opens later (compact <-> main)
SPOKEN_HINT_MARK = "\n\n(This request was spoken."  # voice.SPOKEN_TASK_HINT's start, hidden in history

CLOSED_LABELS = {
    "confirm": {True: "Allowed", False: "Not allowed"},
    "handoff": {True: "Continued", False: "Stopped"},
}


class AppState(NullUi):
    """What the windows show: the session's events, the open question, the
    status. Implements the same ui calls as voice_ui.Overlay, so
    VoiceAssistant drives it unchanged. `push(event)` delivers an event to
    the open windows."""

    def __init__(self, push: Callable[[dict], None] = lambda e: None, on_show: Callable[[], None] = lambda: None):
        self._push = push
        self._on_show = on_show
        self._lock = threading.Lock()
        self.feed: list[dict] = []
        self.question: dict | None = None  # {"qid", "kind", "text"} + the Choice in self._choice
        self._choice: Choice | None = None
        self.status_kind, self.status_text = "ready", "Ready"
        self.is_paused = False

    def emit(self, event: dict) -> None:
        event = {**event, "at": time.time()}
        with self._lock:
            self.feed.append(event)
            del self.feed[:-FEED_LIMIT]
        try:
            self._push(event)
        except Exception:  # noqa: BLE001 -- a window that's gone must never break the task
            pass

    # --- the ui calls VoiceAssistant makes ------------------------------------

    def status(self, kind: str, text: str = "") -> None:
        self.status_kind, self.status_text = kind, text or self.status_text
        self.emit({"type": "status", "kind": kind, "text": self.status_text})

    def heard(self, text: str) -> None:
        self.task(text, "voice")

    def task(self, text: str, source: str) -> None:
        self.emit({"type": "task", "text": text, "source": source})

    def step(self, text: str) -> None:
        self.info(text)

    def agent_step(self, step: int, thought: str, action: str) -> None:
        self.emit({"type": "step", "n": step, "text": thought, "action": action})

    def result(self, text: str, ok: bool) -> None:
        self.status("done" if ok else "failed", "Done" if ok else "That didn't work")
        self.emit({"type": "result", "ok": ok, "text": text})

    def info(self, text: str) -> None:
        self.emit({"type": "info", "text": text})

    def paused(self, paused: bool, takeover: bool = False) -> None:
        self.is_paused = paused
        self.emit({"type": "paused", "paused": paused, "takeover": takeover})

    def show(self) -> None:
        self._on_show()

    def ask(self, prompt: str) -> Choice:
        return self._open("confirm", prompt)

    def ask_text(self, question: str) -> Choice:
        return self._open("question", question)

    def handoff(self, message: str) -> Choice:
        return self._open("handoff", message)

    def end_question(self) -> None:
        with self._lock:
            question, choice = self.question, self._choice
            self.question, self._choice = None, None
        if question is None:
            return
        closing = {"confirm": False, "question": "", "handoff": False}[question["kind"]]
        choice.answer(closing)  # no-op if already answered: the answer is fixed once closed
        value = choice.value
        label = CLOSED_LABELS.get(question["kind"], {}).get(value)
        if question["kind"] == "question":
            label = f"You answered: {value}" if value else "No answer"
        self.emit({"type": "closed", "qid": question["qid"], "label": label})
        if self.status_kind == "asking":
            self.status("working", "Working on it...")

    def _open(self, kind: str, text: str) -> Choice:
        choice = Choice()
        question = {"qid": secrets.token_hex(8), "kind": kind, "text": text}
        with self._lock:
            if self._choice is not None:
                self._choice.answer(None)  # never two open at once
            self.question, self._choice = question, choice
        self.status("asking", {"confirm": "Allow this?", "question": "The agent has a question",
                               "handoff": "Your turn"}[kind])
        self.emit({"type": "ask", **question})
        self.show()
        return choice

    # --- answers from the window ----------------------------------------------

    def answer(self, qid: str, value) -> bool:
        """An answer from a button or the box, for the question `qid` only."""
        with self._lock:
            question, choice = self.question, self._choice
        if question is None or choice is None or question["qid"] != qid:
            return False  # stale: that question is already closed
        kind = question["kind"]
        if kind == "question":
            value = str(value or "").strip()[:1000]
            if not value:
                return False
        else:
            value = value is True
        return choice.answer(value)

    def snapshot(self) -> dict:
        with self._lock:
            return {"feed": list(self.feed), "question": dict(self.question) if self.question else None,
                    "status": {"kind": self.status_kind, "text": self.status_text}, "paused": self.is_paused}


YES_NO = {"yes": True, "no": False}


def route_submit(state: AppState, text: str, busy: bool, add_note: Callable[[str], None],
                 start_task: Callable[[str], bool]) -> str:
    """Where text from the message box goes: an open question's answer, a
    message to the running task, or a new task. Returns what happened:
    "answer", "note", "task", "refused" or "empty"."""
    text = (text or "").strip()
    if not text:
        return "empty"
    question = state.question
    if question is not None:
        if question["kind"] == "question":
            return "answer" if state.answer(question["qid"], text) else "refused"
        word = text.lower().strip(" .!")
        if question["kind"] == "confirm" and word in YES_NO:
            return "answer" if state.answer(question["qid"], YES_NO[word]) else "refused"
        return "refused"  # answer the open question with its buttons first
    if busy:
        add_note(text)
        state.emit({"type": "note", "text": text})
        return "note"
    return "task" if start_task(text) else "refused"


# --- history: the records agent.py saves to output/ -----------------------------

_RUN_ID = re.compile(r"^[\w.-]{1,80}\.json$")


def _clean_task(task: str) -> str:
    task = task.split(SPOKEN_HINT_MARK)[0]
    return task.split("\n\nMESSAGES FROM THE USER DURING THIS TASK")[0].strip()


def load_history(output_dir: Path, limit: int = 60) -> list[dict]:
    """Newest first: {"id", "task", "ok", "summary", "when", "steps"}."""
    runs = []
    for path in sorted(output_dir.glob("*.json"), reverse=True)[: limit * 2]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or "task" not in record:
            continue
        runs.append({
            "id": path.name, "task": _clean_task(str(record.get("task", ""))),
            "ok": record.get("status") == "success", "summary": str(record.get("summary") or "")[:600],
            "when": record.get("saved_at", ""), "steps": record.get("steps_taken", 0),
        })
        if len(runs) >= limit:
            break
    return runs


def load_run(output_dir: Path, run_id: str) -> dict | None:
    """One past run, by the file name load_history() gave -- nothing else."""
    if not _RUN_ID.match(run_id or ""):
        return None
    path = output_dir / run_id
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    steps = (record.get("timings") or {}).get("steps") or []
    return {
        "id": run_id, "task": _clean_task(str(record.get("task", ""))), "ok": record.get("status") == "success",
        "summary": str(record.get("summary") or ""), "when": record.get("saved_at", ""),
        "actions": [{"n": s.get("step"), "action": s.get("action", ""), "decider": s.get("decider", "")}
                    for s in steps if isinstance(s, dict)],
        "artifacts": record.get("artifacts") or [],
        "warnings": record.get("verification_warnings") or [],
    }


def build_page(mode: str) -> str:
    """ui/index.html with its CSS and JS inlined (no local web server)."""
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    css = (UI_DIR / "app.css").read_text(encoding="utf-8")
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    return (html.replace("/*APP_CSS*/", css).replace("/*APP_JS*/", js)
            .replace("__MODE__", "compact" if mode == "compact" else "main"))


# --- the windows ------------------------------------------------------------------

class Api:
    """What the page's JavaScript may call (pywebview js_api). One instance per
    window, so the page knows whether it's the main or the compact one."""

    def __init__(self, app: "App", mode: str):
        self._app, self._mode = app, mode

    def get_state(self) -> dict:
        return {**self._app.state.snapshot(), "mode": self._mode, "busy": self._app.is_busy(),
                "info": self._app.info}

    def submit(self, text: str) -> str:
        return self._app.submit(text)

    def answer(self, qid: str, value) -> bool:
        return self._app.state.answer(qid, value)

    def stop(self) -> None:
        self._app.stop()

    def pause(self) -> None:
        self._app.pause(takeover=False)

    def take_over(self) -> None:
        self._app.pause(takeover=True)

    def resume(self) -> None:
        self._app.resume()

    def mic(self) -> None:
        self._app.on_mic()

    def history(self) -> list:
        return load_history(self._app.output_dir)

    def run_details(self, run_id: str):
        return load_run(self._app.output_dir, run_id)

    def compact(self) -> None:
        self._app.set_mode("compact")

    def expand(self) -> None:
        self._app.set_mode("main")

    def hide(self) -> None:
        self._app.hide_all()

    def quit(self) -> None:
        self._app.quit()


class App:
    """The two pywebview windows (main + compact) around one AppState."""

    def __init__(self, *, is_busy: Callable[[], bool], start_task: Callable[[str], bool],
                 control: Callable[[], object], on_mic: Callable[[], None], output_dir: Path, info: dict):
        import webview

        self._webview = webview
        self.is_busy, self._start_task, self._control, self.on_mic = is_busy, start_task, control, on_mic
        self.output_dir, self.info = output_dir, info
        self.state = AppState(push=self._push, on_show=self.show)
        self.mode = "main"
        self._loaded: set[str] = set()
        self.tray = None
        self._quitting = False
        width, height, work_right, work_bottom = _work_area()
        self.main = webview.create_window(
            APP_TITLE, html=build_page("main"), js_api=Api(self, "main"), width=980, height=680,
            min_size=(720, 480), background_color="#0f1115", text_select=True)
        self.compact_window = webview.create_window(
            COMPACT_TITLE, html=build_page("compact"), js_api=Api(self, "compact"), width=380, height=250,
            x=max(0, work_right - 380 - 16), y=max(0, work_bottom - 250 - 16), frameless=True, on_top=True,
            hidden=True, background_color="#0f1115", easy_drag=False, text_select=True)
        for name, window in (("main", self.main), ("compact", self.compact_window)):
            window.events.loaded += self._mark_loaded(name)
        self.main.events.closing += self._on_main_closing

    def _mark_loaded(self, name: str):
        def loaded() -> None:  # no parameters: pywebview passes none then
            self._loaded.add(name)
        return loaded

    def _windows(self):
        return (("main", self.main), ("compact", self.compact_window))

    def _push(self, event: dict) -> None:
        script = f"window.app && window.app.onEvent({json.dumps(event)})"
        for name, window in self._windows():
            if name in self._loaded:
                try:
                    window.evaluate_js(script)
                except Exception:  # noqa: BLE001
                    pass
        if self.tray is not None and event.get("type") == "status":
            self.tray.set_status(event.get("kind", "ready"))

    def submit(self, text: str) -> str:
        return route_submit(self.state, text, self.is_busy(), lambda t: self._control().add_note(t),
                            self._start_task)

    def stop(self) -> None:
        if self.is_busy():
            self._control().stop()
            self.state.info("Stopping before the next step...")

    def pause(self, takeover: bool) -> None:
        if not self.is_busy():
            return
        self._control().pause()
        self.state.paused(True, takeover)
        self.state.status("paused", "Your turn -- press Continue when done" if takeover else "Paused")

    def resume(self) -> None:
        self._control().resume()
        self.state.paused(False)
        self.state.status("working", "Working on it...")

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        if mode == "compact":
            self.compact_window.show()
            self.main.hide()
        else:
            self.main.show()
            self.compact_window.hide()

    def show(self) -> None:
        (self.compact_window if self.mode == "compact" else self.main).show()

    def hide_all(self) -> None:
        if self.state.question is not None:
            return  # keep a question visible until it's answered
        self.main.hide()
        self.compact_window.hide()

    def _on_main_closing(self):
        if self._quitting or self.tray is None:
            self._quitting = True
            try:
                self.compact_window.destroy()  # or the hidden one would keep the app running, invisible
            except Exception:  # noqa: BLE001 -- already gone
                pass
            return True  # really close (no tray icon to come back from)
        self.hide_all()
        return False  # the X hides it; the icon by the clock brings it back

    def quit(self) -> None:
        self._quitting = True
        for _, window in self._windows():
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass


def _work_area() -> tuple[int, int, int, int]:
    """(screen width, height, work-area right, work-area bottom), i.e. above the taskbar."""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        return rect.right - rect.left, rect.bottom - rect.top, rect.right, rect.bottom
    except Exception:  # noqa: BLE001 -- not Windows: a sensible default
        return 1920, 1080, 1920, 1040


def run_app(app: App, tick: Callable[[], None]) -> None:
    """Start the windows; `tick` (key polling) runs every 20 ms on a helper
    thread until they're closed."""

    def loop() -> None:
        while not app._quitting:
            try:
                tick()
            except Exception as e:  # noqa: BLE001 -- one bad tick must not kill the key loop
                print(f"  [voice] key loop error: {e}")
            time.sleep(0.02)

    def started() -> None:
        threading.Thread(target=loop, daemon=True).start()

    app._webview.start(started, private_mode=True)


def open_path(path: str) -> None:
    if os.path.exists(path) and hasattr(os, "startfile"):
        os.startfile(path)
