"""
Tests for voice.py's logic -- offline, with fakes standing in for the
microphone, the speech model and the speaker. No audio hardware, no model
download, no network. The hardware classes (Recorder, Transcriber, Speaker)
are thin wrappers that can only be checked on a real Windows machine.
"""
import json

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
    confirm = make_voice_confirm(listen=lambda s: "audio", transcribe=lambda a: "Yes.", say=said.append,
                                 log=lambda m: None)
    assert confirm("Ready to click <button 'Submit'>. Continue?") is True
    assert said == ["Ready to click <button 'Submit'>. Continue? Say yes or no."]

    no = make_voice_confirm(lambda s: "audio", lambda a: "hmm, what?", lambda t: None, log=lambda m: None)
    assert no("Continue?") is False


def test_confirm_declines_if_the_microphone_or_model_fails():
    def broken(_seconds):
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

    def run(self, text, config, confirm_callback, should_stop):
        self.runs.append({"text": text, "confirm": confirm_callback, "should_stop": should_stop})
        return {"success": True, "result": "Opened Notepad and typed hello."}


def test_a_spoken_task_runs_through_run_task_and_the_result_is_spoken():
    fakes = _Fakes(["Open Notepad and type hello."])
    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, lambda s: None, fakes.run,
                               log=lambda m: None)

    outcome = assistant.handle_audio("audio")

    assert outcome["success"] is True
    assert fakes.runs[0]["text"].startswith("Open Notepad and type hello.")
    assert "ONE short, plain sentence" in fakes.runs[0]["text"]  # asks Claude for a speakable summary
    assert fakes.said == ["On it.", "Opened Notepad and typed hello."]


def test_silence_or_a_tap_runs_nothing():
    fakes = _Fakes([""])
    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, lambda s: None, fakes.run,
                               log=lambda m: None)
    assert assistant.handle_audio("audio") is None
    assert fakes.runs == []


def test_stop_key_is_wired_to_run_task_and_reset_for_the_next_task():
    fakes = _Fakes(["first task", "second task"])
    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, lambda s: None, fakes.run,
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

    def listen(seconds):
        seen.append(assistant.answering.is_set())
        return "audio"

    assistant = VoiceAssistant(None, fakes.transcribe, fakes.said.append, listen, fakes.run, log=lambda m: None)
    assert assistant.confirm("Continue?") is True
    assert seen == [True] and assistant.answering.is_set() is False


def test_a_failed_task_shows_the_full_reason_on_screen():
    logged = []
    failure = ("WHAT HAPPENED: The AI model could not be reached or gave an unusable reply.\n"
               "WHY: Anthropic request failed: Error code: 529 overloaded\nWHAT YOU CAN DO: retry")
    assistant = VoiceAssistant(None, lambda a: "open notepad", lambda t: None, lambda s: None,
                               lambda *a, **k: {"success": False, "result": failure}, log=logged.append)
    assistant.handle_audio("audio")
    assert any("529 overloaded" in line for line in logged)
