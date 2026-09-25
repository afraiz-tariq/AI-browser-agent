"""
Tests for the app window's logic (app_ui.py), the agent's new interaction
hooks (messages mid-task, ask_user, the login hand-off -- agent.py and
user_tools.py) and the pause/stop control (voice.TaskControl). Offline: no
window, no microphone, a scripted LLM. The page itself (ui/) is checked for
never inserting text as HTML.
"""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import offer_manual_resolution, run_task
from app_ui import AppState, build_page, load_history, load_run, route_submit
from llm import LLMClient, MockProvider
from user_tools import UserToolProvider
from voice import TaskControl, VoiceAssistant

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


# --- AppState: questions and answers --------------------------------------------

def _state():
    pushed = []
    return AppState(push=pushed.append), pushed


def test_an_approval_is_answered_once_and_only_for_its_own_question():
    state, pushed = _state()
    choice = state.ask("Ready to click <button 'Send'>. Continue?")
    qid = state.question["qid"]
    assert state.answer("some-other-id", True) is False  # a stale click from an older card
    assert state.answer(qid, True) is True
    assert state.answer(qid, False) is False  # the first answer stands
    state.end_question()
    assert choice.value is True
    assert pushed[-2] == {**pushed[-2], "type": "closed", "qid": qid, "label": "Allowed"}
    assert state.question is None and state.answer(qid, True) is False


def test_only_a_real_true_counts_as_allow():
    state, _ = _state()
    choice = state.ask("Continue?")
    state.answer(state.question["qid"], "yes")  # a string from the page is not True
    assert choice.value is False


def test_a_closed_question_without_an_answer_is_a_no():
    state, pushed = _state()
    choice = state.ask("Continue?")
    state.end_question()
    assert choice.value is False
    assert any(e.get("label") == "Not allowed" for e in pushed)


def test_the_agents_question_takes_typed_text():
    state, pushed = _state()
    choice = state.ask_text("Which tab?")
    qid = state.question["qid"]
    assert state.answer(qid, "   ") is False  # empty isn't an answer
    assert state.answer(qid, "the second one") is True
    state.end_question()
    assert choice.value == "the second one"
    assert any(e.get("label") == "You answered: the second one" for e in pushed)


def test_a_new_question_closes_an_old_one_with_no_answer():
    state, _ = _state()
    first = state.ask("First?")
    state.ask("Second?")
    assert first.decided and first.value is None


def test_a_broken_window_never_breaks_the_task():
    def push(event):
        raise RuntimeError("window gone")

    state = AppState(push=push)
    state.status("working", "Working on it...")
    state.agent_step(1, "Opening Notepad.", "windows_launch_app")
    assert [e["type"] for e in state.feed] == ["status", "step"]


def test_the_feed_is_kept_for_a_window_that_opens_later():
    state, _ = _state()
    state.task("open notepad", "typed")
    state.ask("Continue?")
    snap = state.snapshot()
    assert snap["feed"][0]["text"] == "open notepad"
    assert snap["question"]["kind"] == "confirm"
    assert snap["status"]["kind"] == "asking"


# --- what the message box does -----------------------------------------------------

def _route(state, text, busy=False):
    notes, tasks = [], []
    result = route_submit(state, text, busy, notes.append, lambda t: tasks.append(t) or True)
    return result, notes, tasks


def test_idle_text_starts_a_task():
    state, _ = _state()
    assert _route(state, " open notepad ") == ("task", [], ["open notepad"])
    assert _route(state, "   ")[0] == "empty"


def test_text_during_a_task_is_a_message_to_it():
    state, _ = _state()
    result, notes, tasks = _route(state, "use the other tab", busy=True)
    assert (result, notes, tasks) == ("note", ["use the other tab"], [])
    assert state.feed[-1]["type"] == "note"


def test_text_answers_an_open_question_first():
    state, _ = _state()
    choice = state.ask_text("Which one?")
    assert _route(state, "ABC News", busy=True)[0] == "answer"
    assert choice.value == "ABC News"


def test_only_a_plain_yes_or_no_answers_an_approval_from_the_box():
    state, _ = _state()
    choice = state.ask("Continue?")
    assert _route(state, "go on then", busy=True) == ("refused", [], [])  # not a note either
    assert not choice.decided
    assert _route(state, "Yes.", busy=True)[0] == "answer"
    assert choice.value is True


# --- history -----------------------------------------------------------------------

def _record(folder, name, **fields):
    (folder / name).write_text(json.dumps(fields), encoding="utf-8")


def test_history_lists_past_runs_newest_first_without_the_spoken_hint(tmp_path):
    _record(tmp_path, "2026-09-24_100000.json", task="old task", status="success", summary="ok", steps_taken=2)
    _record(tmp_path, "2026-09-25_100000.json",
            task="open notepad\n\n(This request was spoken. When you finish, make the summary ONE short...)",
            status="failed", summary="stuck", steps_taken=12,
            timings={"steps": [{"step": 1, "action": "windows_launch_app"}]})
    (tmp_path / "2026-09-25_110000.json").write_text("{not json", encoding="utf-8")
    runs = load_history(tmp_path)
    assert [r["task"] for r in runs] == ["open notepad", "old task"]
    assert runs[0]["ok"] is False and runs[1]["ok"] is True
    detail = load_run(tmp_path, runs[0]["id"])
    assert detail["actions"] == [{"n": 1, "action": "windows_launch_app", "decider": ""}]


@pytest.mark.parametrize("run_id", ["../secrets.json", "..\\x.json", "a/b.json", "notes.txt", ""])
def test_a_past_run_is_only_read_by_its_own_file_name(tmp_path, run_id):
    assert load_run(tmp_path, run_id) is None


# --- the page ------------------------------------------------------------------------

def test_the_page_never_inserts_text_as_html():
    # Steps and results carry text read from web pages; this window can
    # answer approvals. Nothing may become markup or script.
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert risky not in js, risky


@pytest.mark.parametrize("mode", ["main", "compact"])
def test_the_page_is_one_self_contained_file(mode):
    page = build_page(mode)
    assert f'data-mode="{mode}"' in page
    assert "/*APP_JS*/" not in page and "/*APP_CSS*/" not in page
    assert "window.app = " in page and ".composer" in page
    for load in ('src="http', "href=\"http", "url(http", "fetch(", "XMLHttpRequest", "@import"):
        assert load not in page, load  # nothing loaded from anywhere


# --- the agent's side: messages mid-task, ask_user, the login hand-off ----------------

def _reply(thought, action, args):
    return json.dumps({"thought": thought, "action": action, "args": args})


def test_messages_sent_mid_task_reach_the_model_inside_the_task(test_config, fixtures_server):
    mock = MockProvider([
        _reply("Opening.", "goto", {"url": f"{fixtures_server}/index.html"}),
        _reply("Done.", "finish", {"summary": "The mock search engine page has a search box."}),
    ])
    inbox = [["use the second result instead"]]
    outcome = run_task("Search for openai.", test_config, dry_run=True, llm_client=LLMClient(mock),
                       task_updates=lambda: inbox.pop() if inbox else [])
    assert outcome["success"] is True
    first, second = mock.calls[0][1], mock.calls[1][1]
    assert "use the second result instead" in first.split("ACTION HISTORY")[0]  # in the TASK part
    assert "MESSAGES FROM THE USER DURING THIS TASK" in second


def test_the_agent_can_ask_and_the_answer_joins_the_task(test_config, fixtures_server):
    asked = []
    mock = MockProvider([
        _reply("Two results match; asking.", "ask_user", {"question": "ABC News or the ABC song?"}),
        _reply("Done.", "finish", {"summary": "The person wanted ABC News, so that is what I looked for."}),
    ])
    outcome = run_task("Find ABC.", test_config, dry_run=True, llm_client=LLMClient(mock),
                       ask_user=lambda q: asked.append(q) or "ABC News")
    assert outcome["success"] is True
    assert asked == ["ABC News or the ABC song?"]
    task_part = mock.calls[1][1].split("ACTION HISTORY")[0]
    assert '(answering your question "ABC News or the ABC song?") ABC News' in task_part


def test_without_an_interactive_front_end_there_is_no_ask_user_tool():
    specs = UserToolProvider(lambda q: "x", lambda q, a: None).get_tool_specs()
    assert [(s.name, s.risk_level) for s in specs] == [("ask_user", "R0")]  # classified, not the R3 default


def test_an_unanswered_question_says_so_and_adds_nothing():
    answers = []
    provider = UserToolProvider(lambda q: None, lambda q, a: answers.append(a))
    assert "didn't answer" in provider.execute("ask_user", {"question": "Which tab?"})
    assert answers == []


class _Logger:
    def note(self, message):
        pass


@pytest.mark.parametrize("pressed, expected", [(True, True), (False, False)])
def test_a_login_wall_is_handed_to_the_person_through_the_app(pressed, expected):
    config = SimpleNamespace(headless=False)
    seen = []
    result = offer_manual_resolution("https://example.com/login", config, False, _Logger(), 1,
                                     handoff=lambda message: seen.append(message) or pressed)
    assert result is expected
    assert "example.com/login" in seen[0] and "Continue" in seen[0]


def test_the_hand_off_never_happens_headless():
    config = SimpleNamespace(headless=True)
    assert offer_manual_resolution("https://x", config, False, _Logger(), 1, handoff=lambda m: True) is False


# --- pause / stop ------------------------------------------------------------------------

def test_pause_holds_the_agent_until_continue():
    control = TaskControl()
    control.pause()
    done = []
    worker = threading.Thread(target=lambda: done.append(control.should_stop()))
    worker.start()
    time.sleep(0.3)
    assert done == []  # held between steps
    control.resume()
    worker.join(2)
    assert done == [False]


def test_stop_wakes_a_paused_task_and_stops_it():
    control = TaskControl()
    control.pause()
    done = []
    worker = threading.Thread(target=lambda: done.append(control.should_stop()))
    worker.start()
    control.stop()
    worker.join(2)
    assert done == [True]
    control.reset()
    assert control.should_stop() is False and not control.paused


def test_messages_are_handed_over_once():
    control = TaskControl()
    control.add_note(" use tab 2 ")
    control.add_note("  ")
    assert control.take_notes() == ["use tab 2"]
    assert control.take_notes() == []


# --- the agent asking, through the voice assistant ---------------------------------------

class _Ui:
    def __init__(self, typed=None):
        from voice_ui import Choice, NullUi

        self._null, self._choice_cls, self.typed, self.events = NullUi(), Choice, typed, []

    def __getattr__(self, name):
        return getattr(self._null, name)

    def ask_text(self, question):
        choice = self._choice_cls()
        if self.typed is not None:
            choice.answer(self.typed)
        self.events.append(("ask_text", question))
        return choice

    def handoff(self, message):
        choice = self._choice_cls()
        choice.answer(True)
        return choice

    def end_question(self):
        self.events.append(("end",))


def _assistant(ui, heard=""):
    return VoiceAssistant(None, lambda audio: heard, lambda text: None, lambda s, abort=None: "audio",
                          lambda *a, **k: {"success": True, "result": "ok"}, log=lambda m: None, ui=ui)


def test_a_typed_answer_to_the_agents_question_is_used():
    ui = _Ui(typed="the second tab")
    assert _assistant(ui).ask_user("Which tab?") == "the second tab"
    assert ui.events[-1] == ("end",)


def test_a_spoken_answer_to_the_agents_question_is_used():
    assert _assistant(_Ui(), heard="The first one.").ask_user("Which tab?") == "The first one."


def test_no_window_means_no_hand_off():
    from voice_ui import NullUi

    assert _assistant(NullUi()).handoff("log in please") is False
    assert _assistant(_Ui()).handoff("log in please") is True


# --- the App shell, against a stand-in for pywebview ------------------------------------

class _Event(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


class _Window:
    def __init__(self, title, **kwargs):
        self.title, self.kwargs, self.scripts, self.visible = title, kwargs, [], not kwargs.get("hidden")
        self.events = SimpleNamespace(loaded=_Event(), closing=_Event())

    def evaluate_js(self, script):
        self.scripts.append(script)

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def destroy(self):
        self.visible = False


@pytest.fixture
def app(monkeypatch, tmp_path):
    import sys

    windows = []
    fake = SimpleNamespace(create_window=lambda title, **kw: windows.append(_Window(title, **kw)) or windows[-1],
                           start=lambda func, **kw: func())
    monkeypatch.setitem(sys.modules, "webview", fake)
    from app_ui import App

    control = TaskControl()
    started = []
    busy = {"v": False}
    application = App(is_busy=lambda: busy["v"], start_task=lambda t: started.append(t) or True,
                      control=lambda: control, on_mic=lambda: None, output_dir=tmp_path,
                      info={"talk_key": "ctrl_r"})
    application._test = SimpleNamespace(windows=windows, control=control, started=started, busy=busy)
    return application


def test_the_app_opens_a_main_window_and_a_hidden_compact_one(app):
    main, compact = app._test.windows
    assert (main.title, compact.title) == ("AI Agent", "AI Agent (compact)")
    assert main.visible and not compact.visible
    assert compact.kwargs["on_top"] and compact.kwargs["frameless"]


def test_events_reach_only_windows_that_have_loaded(app):
    main, compact = app._test.windows
    for handler in main.events.loaded:
        handler()
    app.state.status("working", "Working on it...")
    assert len(main.scripts) == 1 and compact.scripts == []
    assert json.loads(main.scripts[0].split("onEvent(", 1)[1].rstrip(")"))["kind"] == "working"


def test_compact_mode_swaps_the_windows(app):
    main, compact = app._test.windows
    app.set_mode("compact")
    assert compact.visible and not main.visible
    app.set_mode("main")
    assert main.visible and not compact.visible


def test_closing_the_main_window_hides_it_when_there_is_a_tray_icon(app):
    main, _ = app._test.windows
    app.tray = object()
    assert all(handler() is False for handler in main.events.closing)
    assert not main.visible
    app.tray = None
    compact = app._test.windows[1]
    compact.show()
    assert all(handler() is True for handler in main.events.closing)  # no icon to come back from: quit
    assert not compact.visible and app._quitting  # the compact one goes too, or the app lingers unseen


def test_pause_and_stop_drive_the_running_task(app):
    app._test.busy["v"] = True
    app.pause(takeover=True)
    assert app._test.control.paused and app.state.is_paused
    app.resume()
    assert not app._test.control.paused
    app.stop()
    assert app._test.control.stopped


def test_the_box_starts_a_task_or_messages_a_running_one(app):
    assert app.submit("open notepad") == "task" and app._test.started == ["open notepad"]
    app._test.busy["v"] = True
    assert app.submit("the other tab") == "note"
    assert app._test.control.take_notes() == ["the other tab"]
