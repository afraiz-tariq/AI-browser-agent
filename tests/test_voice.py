"""
Tests for voice.py's logic -- offline, with fakes standing in for the
microphone, the speech model and the speaker. No audio hardware, no model
download, no network. The hardware classes (Recorder, Transcriber, Speaker)
are thin wrappers that can only be checked on a real Windows machine.
"""
import json
import os

import pytest

from agent import run_task
from llm import LLMClient, MockProvider
from voice import VoiceAssistant, is_yes, make_voice_confirm, short_for_speech


@pytest.mark.parametrize("heard", ["yes", "Yes.", " YES! ", "confirm", "Confirm."])
def test_plain_yes_or_confirm_counts(heard):
    assert is_yes(heard) is True


@pytest.mark.parametrize("heard", [
    "", "no", "No.", "yeah", "yes please", "yes, but first open the other one", "not yes", "maybe", "confirmed",
])
def test_anything_else_is_a_no(heard):
    # A risky action never proceeds on an ambiguous answer.
    assert is_yes(heard) is False


def test_confirm_asks_aloud_then_listens_and_only_yes_continues():
    said = []
    confirm = make_voice_confirm(listen=lambda s, abort=None: "audio", transcribe=lambda a: "Yes.", say=said.append,
                                 log=lambda m: None)
    assert confirm("Ready to click <button 'Submit'>. Continue?") is True
    assert said == ["Ready to click <button 'Submit'>. Continue? Say yes or no."]

    no = make_voice_confirm(lambda s, abort=None: "audio", lambda a: "hmm, what?", lambda t: None, log=lambda m: None)
    assert no("Continue?") is False


def test_silence_gets_asked_once_more_instead_of_counting_as_no():
    # The first voice search declined on '' although the user never said no.
    said = []
    answers = iter(["", "Yes."])
    confirm = make_voice_confirm(lambda s, abort=None: "audio", lambda a: next(answers), said.append, log=lambda m: None)
    assert confirm("Ready to type into <textarea> and submit. Continue?") is True
    assert said[1] == "I didn't hear an answer. Say yes or no."


def test_silence_twice_still_declines_and_a_no_is_not_asked_again():
    said = []
    confirm = make_voice_confirm(lambda s, abort=None: "audio", lambda a: "", said.append, log=lambda m: None)
    assert confirm("Continue?") is False
    assert len(said) == 2

    said.clear()
    confirm = make_voice_confirm(lambda s, abort=None: "audio", lambda a: "No.", said.append, log=lambda m: None)
    assert confirm("Continue?") is False
    assert len(said) == 1  # a real answer is final


def test_confirm_declines_if_the_microphone_or_model_fails():
    def broken(_seconds, abort=None):
        raise OSError("no microphone")

    confirm = make_voice_confirm(broken, lambda a: "yes", lambda t: None, log=lambda m: None)
    assert confirm("Continue?") is False


def test_results_are_shortened_for_speech():
    assert short_for_speech("WHAT HAPPENED: Stopped by you.\nWHY: stop key\nWHAT YOU CAN DO: x") == "Stopped by you."
    long = "First sentence here. " * 40
    spoken = short_for_speech(long)
    assert len(spoken) <= 220 and spoken.endswith(".")
    # The first voice run read a whole technical summary aloud; now only the
    # first sentence is spoken.
    summary = 'Opened the Calculator app and computed 3 + 2. The result is 5 (Expression: "3 + 2=", Display: "5").'
    assert short_for_speech(summary) == "Opened the Calculator app and computed 3 + 2."


class _Fakes:
    def __init__(self, transcripts):
        self.transcripts = list(transcripts)
        self.said = []
        self.runs = []

    def transcribe(self, audio):
        return self.transcripts.pop(0) if self.transcripts else ""

    def run(self, text, config, confirm_callback, should_stop, **hooks):
        self.runs.append({"text": text, "confirm": confirm_callback, "should_stop": should_stop})
        return {"success": True, "result": "Opened Notepad and typed hello."}


def test_a_spoken_task_runs_through_run_task_and_the_result_is_spoken():
    fakes = _Fakes(["Open Notepad and type hello."])
    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, lambda s, abort=None: None, fakes.run,
                               log=lambda m: None)

    outcome = assistant.handle_audio("audio")

    assert outcome["success"] is True
    assert fakes.runs[0]["text"].startswith("Open Notepad and type hello.")
    assert "ONE short, plain sentence" in fakes.runs[0]["text"]  # asks Claude for a speakable summary
    assert fakes.said == ["On it.", "Opened Notepad and typed hello."]


def test_silence_or_a_tap_runs_nothing():
    fakes = _Fakes([""])
    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, lambda s, abort=None: None, fakes.run,
                               log=lambda m: None)
    assert assistant.handle_audio("audio") is None
    assert fakes.runs == []


def test_stop_key_is_wired_to_run_task_and_reset_for_the_next_task():
    fakes = _Fakes(["first task", "second task"])
    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, lambda s, abort=None: None, fakes.run,
                               log=lambda m: None)
    assistant.handle_audio("a")
    assistant.request_stop()
    assert fakes.runs[0]["should_stop"]() is True
    assistant.handle_audio("b")  # a new task must not start already stopped
    assert fakes.runs[1]["should_stop"]() is False


def _reply(thought, action, args):
    return json.dumps({"thought": thought, "action": action, "args": args})


def test_should_stop_halts_the_real_loop_before_the_next_action(test_config, fixtures_server):
    # The stop key's effect on the actual agent loop: after the first step,
    # stopping means no further action runs and the task ends "stopped by you".
    mock = MockProvider([
        _reply("Opening.", "goto", {"url": f"{fixtures_server}/index.html"}),
        _reply("Typing.", "type", {"index": 0, "text": "openai", "submit": True}),
    ])
    calls = {"n": 0}

    def stop_after_first_step():
        calls["n"] += 1
        return calls["n"] > 2  # checks: before step 1, before its action, then before step 2 -> stop

    outcome = run_task("Search for openai.", test_config, dry_run=True, llm_client=LLMClient(mock),
                       should_stop=stop_after_first_step)

    assert outcome["success"] is False
    assert "Stopped by you" in outcome["result"]
    assert len(mock.calls) == 1  # the second decision was never even asked for


def test_a_stop_pressed_during_the_decision_prevents_that_action(test_config, fixtures_server):
    # Deciding can take seconds; a stop pressed meanwhile must win over the
    # action just decided -- here the very first goto never runs.
    mock = MockProvider([_reply("Opening.", "goto", {"url": f"{fixtures_server}/index.html"})])
    calls = {"n": 0}

    def stop_during_first_decision():
        calls["n"] += 1
        return calls["n"] >= 2  # False before step 1, True right before its action

    outcome = run_task("Open the page.", test_config, dry_run=True, llm_client=LLMClient(mock),
                       should_stop=stop_during_first_decision)

    assert outcome["success"] is False and "Stopped by you" in outcome["result"]
    record = json.loads(open(outcome["output_path"], encoding="utf-8").read())
    assert record["artifacts"] == []  # the goto was never executed


# --- push-to-talk key handling (Windows key polling, faked here) ------------

from voice import PushToTalk, key_code  # noqa: E402


def test_key_names_map_to_windows_virtual_key_codes():
    assert key_code("ctrl_r") == 0xA3
    assert key_code("F9") == 0x78 and key_code("f10") == 0x79
    assert key_code("alt_gr") == 0xA5
    assert key_code("k") == ord("K") and key_code("5") == ord("5")
    with pytest.raises(ValueError):
        key_code("definitely_not_a_key")


def test_holding_the_talk_key_starts_once_and_releasing_sends():
    ptt = PushToTalk()
    samples = [(False, False), (True, False), (True, False), (True, False), (False, False), (False, False)]
    events = [ptt.update(talk, stop) for talk, stop in samples]
    # Held for three samples (~60 ms each poll): one start, one send -- no
    # repeats while held, the way a key-repeat would have produced.
    assert events == [[], ["start"], [], [], ["send"], []]


def test_stop_key_fires_once_per_press():
    ptt = PushToTalk()
    events = [ptt.update(False, stop) for stop in (False, True, True, False, True)]
    assert events == [[], ["stop"], [], [], ["stop"]]


def test_assistant_flags_when_it_is_listening_for_a_yes_or_no():
    seen = []
    fakes = _Fakes(["Yes."])

    def listen(seconds, abort=None):
        seen.append(assistant.answering.is_set())
        return "audio"

    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, listen, fakes.run, log=lambda m: None)
    assert assistant.confirm("Continue?") is True
    assert seen == [True] and assistant.answering.is_set() is False


def test_a_failed_task_shows_the_full_reason_on_screen():
    logged = []
    failure = ("WHAT HAPPENED: The AI model could not be reached or gave an unusable reply.\n"
               "WHY: Anthropic request failed: Error code: 529 overloaded\nWHAT YOU CAN DO: retry")
    assistant = VoiceAssistant(None, lambda a: "open notepad", lambda t: None, lambda s, abort=None: None,
                               lambda *a, **k: {"success": False, "result": failure}, log=logged.append)
    assistant.handle_audio("audio")
    assert any("529 overloaded" in line for line in logged)


# --- tap-to-talk and running without a terminal ------------------------------

from voice import AUTOSTART_NAME, SpeechEndDetector, check_install, ensure_env_file, set_autostart, talk_key_action  # noqa: E402

CHUNK = 0.1  # seconds per fake audio chunk


def _feed(detector, peaks):
    for peak in peaks:
        if detector.done:
            break
        detector.add(peak, CHUNK)
    return detector


def test_a_tap_recording_ends_about_a_second_after_you_stop_talking():
    quiet, speech = 0.005, 0.2  # the user's mic measured ~0.005 in silence
    d = _feed(SpeechEndDetector(), [quiet] * 5 + [speech] * 15 + [quiet] * 9)
    assert d.heard_speech and not d.done  # 0.9 s of quiet: a pause, keep listening
    _feed(d, [quiet])
    assert d.done  # 1.0 s of quiet after speech: send it


def test_a_pause_mid_sentence_does_not_end_the_recording():
    d = _feed(SpeechEndDetector(), [0.2] * 10 + [0.005] * 6 + [0.2] * 10 + [0.005] * 5)
    assert not d.done


def test_a_tap_with_no_speech_gives_up_and_says_nothing_was_heard():
    d = _feed(SpeechEndDetector(wait_for_speech=6.0), [0.005] * 100)
    assert d.done and not d.heard_speech
    assert round(d.elapsed, 1) == 6.0


def test_a_noisy_room_raises_the_speech_threshold():
    # Background at 0.03 is above the fixed 0.02 floor, but it's the room,
    # not you: speech must beat 3x the quietest chunk.
    d = _feed(SpeechEndDetector(), [0.03, 0.04, 0.05] * 3)
    assert not d.heard_speech
    _feed(d, [0.3])
    assert d.heard_speech


def test_a_recording_never_runs_past_the_maximum():
    d = _feed(SpeechEndDetector(max_seconds=20.0), [0.3] * 300)  # never stops talking
    assert d.done and round(d.elapsed, 1) == 20.0


@pytest.mark.parametrize("mode, event, recording, expected", [
    ("tap", "start", False, "start"),   # tap: begin
    ("tap", "start", True, "send"),     # tap again: send now, don't wait for the quiet
    ("tap", "send", True, None),        # letting go of the key does nothing in tap mode
    ("hold", "start", False, "start"),
    ("hold", "send", True, "send"),     # hold mode: release sends, as before
    ("tap", "stop", False, None),
])
def test_what_the_talk_key_does(mode, event, recording, expected):
    assert talk_key_action(mode, event, recording) == expected


def test_autostart_adds_and_removes_a_minimized_launcher(tmp_path):
    startup = tmp_path / "Startup"
    message = set_autostart(True, str(startup), r"D:\AI-Agent-latest")
    launcher = startup / AUTOSTART_NAME
    assert "will start" in message
    bat = os.path.join(r"D:\AI-Agent-latest", "start_voice.bat")
    assert launcher.read_bytes().decode() == f'@echo off\r\nstart "AI Agent voice" /min "{bat}"\r\n'

    assert "Removed" in set_autostart(False, str(startup), r"D:\AI-Agent-latest")
    assert not launcher.exists()
    assert "nothing to remove" in set_autostart(False, str(startup), r"D:\AI-Agent-latest")


def test_autostart_for_the_packaged_exe_launches_the_exe_itself(tmp_path):
    startup = tmp_path / "Startup"
    exe = r"D:\AI Agent\AI Agent.exe"
    set_autostart(True, str(startup), r"D:\AI Agent", target=exe)
    assert (startup / AUTOSTART_NAME).read_bytes().decode() == f'@echo off\r\nstart "AI Agent voice" /min "{exe}"\r\n'


def test_first_run_of_the_exe_copies_env_example_next_to_it(tmp_path):
    app, bundle = tmp_path / "app", tmp_path / "bundle"
    app.mkdir()
    bundle.mkdir()
    (bundle / ".env.example").write_text("LLM_PROVIDER=anthropic\n", encoding="utf-8")
    created = ensure_env_file(app, bundle)
    assert created == app / ".env"
    assert created.read_text(encoding="utf-8") == "LLM_PROVIDER=anthropic\n"
    # A .env the person already filled in is never overwritten.
    created.write_text("MY_KEY=1\n", encoding="utf-8")
    assert ensure_env_file(app, bundle) is None
    assert created.read_text(encoding="utf-8") == "MY_KEY=1\n"


def test_check_install_fails_only_on_a_missing_required_module(capsys):
    assert check_install(required=("json",), optional=("no_such_module_xyz",), driver_check=None) == 0
    assert check_install(required=("json", "no_such_module_xyz"), optional=(), driver_check=None) == 1
    assert "MISSING  no_such_module_xyz" in capsys.readouterr().out


def test_check_install_fails_when_the_browser_driver_is_missing(capsys):
    # The first exe build: playwright imported fine, but its Node.js driver
    # wasn't bundled, so browser tasks would all have failed.
    assert check_install(required=("json",), optional=(), driver_check=lambda: "not found: node.exe") == 1
    assert "MISSING  the browser driver" in capsys.readouterr().out
    assert check_install(required=("json",), optional=(), driver_check=lambda: None) == 0


def test_the_real_driver_check_finds_this_machines_playwright():
    from voice import playwright_driver_problem

    assert playwright_driver_problem() is None


def test_the_launcher_script_runs_voice_with_the_projects_own_python():
    from pathlib import Path

    bat = (Path(__file__).resolve().parent.parent / "start_voice.bat").read_bytes()
    assert b"\r\n" in bat  # Windows line endings
    assert b'".venv\\Scripts\\python.exe" voice.py' in bat
    assert b'start "" ".venv\\Scripts\\pythonw.exe" voice.py' in bat  # no terminal window when it can
    assert b'cd /d "%~dp0"' in bat  # works wherever the project folder is


# --- the floating window: Yes / No buttons, status -----------------------------

from voice_ui import Choice, NullUi  # noqa: E402
from voice import result_for_window  # noqa: E402


def test_the_first_button_answer_wins():
    choice = Choice()
    assert choice.wait(0.01) is None and not choice.decided
    assert choice.answer(True) is True
    assert choice.answer(False) is False  # a second click changes nothing
    assert choice.value is True and choice.wait(0) is True


class _FakeUi(NullUi):
    """Records what the window was told; `click` answers the question at a
    chosen moment: 'before' the spoken question ends, 'during' listening."""

    def __init__(self, click=None, when="during"):
        self.events, self.click, self.when, self.choice = [], click, when, None

    def ask(self, prompt):
        self.events.append(("ask", prompt))
        self.choice = Choice()
        return self.choice

    def end_question(self):
        self.events.append(("end",))

    def status(self, kind, text=""):
        self.events.append(("status", kind))

    def heard(self, text):
        self.events.append(("heard", text))

    def step(self, text):
        self.events.append(("step", text))

    def result(self, text, ok):
        self.events.append(("result", text, ok))


def _confirm_with(ui, transcripts, click_wait=0.05):
    transcribed, said = [], []
    answers = iter(transcripts)

    def say(text):
        said.append(text)
        if ui.click is not None and ui.when == "before":
            ui.choice.answer(ui.click)

    def listen(seconds, abort):
        if ui.click is not None and ui.when == "during":
            ui.choice.answer(ui.click)
            assert abort() is True  # the recording is told to stop at once
        return "audio"

    def transcribe(audio):
        transcribed.append(audio)
        return next(answers, "")

    confirm = make_voice_confirm(listen, transcribe, say, log=lambda m: None, ui=ui, click_wait=click_wait)
    return confirm, said, transcribed


@pytest.mark.parametrize("when", ["before", "during"])
@pytest.mark.parametrize("click", [True, False])
def test_a_click_answers_the_question(click, when):
    ui = _FakeUi(click=click, when=when)
    confirm, said, transcribed = _confirm_with(ui, [])
    assert confirm("Ready to click <button 'Send'>. Continue?") is click
    assert transcribed == []  # the recording isn't even transcribed
    assert said[0].endswith("Say yes or no, or click a button.")
    assert ui.events[-1] == ("end",)  # the buttons go away


def test_a_spoken_answer_still_works_with_the_window_open():
    ui = _FakeUi()
    confirm, _, _ = _confirm_with(ui, ["Yes."])
    assert confirm("Continue?") is True
    confirm, _, _ = _confirm_with(ui, ["No."])
    assert confirm("Continue?") is False
    assert ui.choice.decided  # closing the question answers it (no), so a late click can't count


def test_silence_leaves_the_buttons_up_for_a_while_then_declines():
    ui = _FakeUi()
    confirm, said, _ = _confirm_with(ui, ["", ""], click_wait=0.05)
    assert confirm("Continue?") is False  # nobody said or clicked anything
    assert len(said) == 2


def test_a_click_after_the_silence_still_counts():
    ui = _FakeUi()
    confirm, _, _ = _confirm_with(ui, ["", ""], click_wait=2)
    import threading
    threading.Timer(0.05, lambda: ui.choice.answer(True)).start()
    assert confirm("Continue?") is True


def test_the_window_follows_the_task():
    ui = _FakeUi()
    steps = []

    def run(text, config, confirm_callback, should_stop, on_step=None, **hooks):
        on_step(1, "Opening Google.", "goto")
        steps.append(text)
        return {"success": True, "result": "Searched Google for ABC. Results are showing."}

    assistant = VoiceAssistant(None, lambda a: "search google for ABC", lambda t: None, lambda s, a=None: None, run,
                               log=lambda m: None, ui=ui)
    assistant.handle_audio("audio")
    assert ("heard", "search google for ABC") in ui.events
    assert ("step", "Step 1: Opening Google.") in ui.events
    assert ui.events[-1] == ("result", "Searched Google for ABC.", True)


def test_a_failure_shows_what_happened_and_why_but_not_the_long_advice():
    outcome = {"success": False, "result": "WHAT HAPPENED: Chrome could not be launched.\n"
                                           "WHY: Chrome may not be installed.\nWHAT YOU CAN DO: Install it."}
    assert result_for_window(outcome) == "Chrome could not be launched. Chrome may not be installed."


def test_the_tray_icon_is_a_dot_in_the_status_colour():
    pytest.importorskip("PIL")
    from voice_ui import STATUS_COLORS, icon_image

    image = icon_image(STATUS_COLORS["listening"])
    assert image.size == (64, 64)
    assert image.getpixel((32, 32))[:3] == (0xEF, 0x44, 0x44)  # red while listening
    assert image.getpixel((1, 1))[3] == 0  # transparent corners


# --- typing a task instead of saying it ----------------------------------------

import queue  # noqa: E402

from voice import submit_typed  # noqa: E402


def test_a_typed_task_takes_the_same_path_as_a_spoken_one():
    fakes = _Fakes([])  # nothing to transcribe: typed text isn't audio
    ui = _FakeUi()
    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, lambda s, a=None: None, fakes.run,
                               log=lambda m: None, ui=ui)
    outcome = assistant.handle_job(("text", "Open Notepad and type hello."))
    assert outcome["success"] is True
    assert fakes.runs[0]["text"].startswith("Open Notepad and type hello.")
    assert fakes.runs[0]["confirm"] == assistant.confirm  # same confirmations as speech
    assert ("heard", "Open Notepad and type hello.") in ui.events


def test_a_typed_quick_command_is_instant_too():
    from tests.test_quick_commands import _quick

    quick, done = _quick()
    fakes = _Fakes([])
    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, lambda s, a=None: None, fakes.run,
                               log=lambda m: None, quick=quick)
    assert assistant.handle_job(("text", "volume up"))["quick"] is True
    assert fakes.runs == [] and done[0] == ("key", "volume_up")


def test_a_typed_task_waits_its_turn_it_is_not_queued_behind_a_running_one():
    jobs, state = queue.Queue(), {"busy": False}
    assert submit_typed("  open notepad ", state, jobs) is True
    assert jobs.get_nowait() == ("text", "open notepad")
    # Still busy: refused, so nothing (the agent included, typing into the
    # window while it runs) can line up another task.
    assert submit_typed("open calculator", state, jobs) is False
    assert jobs.empty()
    state["busy"] = False
    assert submit_typed("   ", state, jobs) is False  # nothing typed


# --- the packaged exe on a real PC ------------------------------------------------

def test_unblocking_does_nothing_off_windows_or_when_nothing_is_marked(tmp_path, monkeypatch):
    from voice import unblock_bundle

    (tmp_path / "pythonnet" / "runtime").mkdir(parents=True)
    (tmp_path / "pythonnet" / "runtime" / "Python.Runtime.dll").write_bytes(b"x")
    assert unblock_bundle(tmp_path) == 0  # not Windows here
    monkeypatch.setattr("voice.sys.platform", "win32")
    assert unblock_bundle(tmp_path) == 0  # no Zone.Identifier mark on the probe file


def test_a_broken_app_window_backend_means_the_small_window_not_a_crash(monkeypatch):
    import builtins

    from voice import app_window_problem

    monkeypatch.setattr("voice.sys.platform", "win32")
    real_import = builtins.__import__

    def failing(name, *args, **kwargs):
        if name.startswith("webview.platforms"):
            raise RuntimeError("Failed to resolve Python.Runtime.Loader.Initialize")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing)
    assert "Python.Runtime.Loader.Initialize" in app_window_problem()
