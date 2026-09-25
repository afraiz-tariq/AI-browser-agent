#!/usr/bin/env python3
"""
Voice front-end: tap a key, say a task, the agent does it and says the result.

    python voice.py
    start_voice.bat              # the same, without opening a terminal or activating .venv
    python voice.py --autostart on    # start it whenever you log in to Windows (off: undo)
    python voice.py --keys       # troubleshooting: show which keys this program sees
    python voice.py --mic-test   # troubleshooting: record 4 s and show what was heard

Tap VOICE_PTT_KEY (default: right Ctrl) and speak; recording ends by itself
when you stop talking (or tap again). Or type the task: in the window's
text box, or in the terminal, and press Enter -- the same path either way. VOICE_MODE=hold restores hold-to-talk.
Press VOICE_STOP_KEY (default: F10) to stop a running task between steps.
Ctrl+C in this window (or closing it) quits.

Like discord_bot.py, this is only another way to call run_task(): the loop,
the arms, the risk tiers and every confirmation are the same ones
`python agent.py` uses. What's different is *how you're asked*: a
confirmation is spoken aloud and answered by voice, and only a plain "yes"
or "confirm" continues -- silence, anything else, or anything that can't be
heard declines (fail-closed, same as a typed "n").

Privacy by design (docs/JEV_VOICE_PLAN.md Phase 3):
- Push-to-talk: the microphone records only after the key is tapped (until
  you stop speaking) or while it's held, and for a few seconds after a
  spoken confirmation question. Nothing is always-on.
- Speech-to-text runs locally (faster-whisper). Audio never leaves this
  machine and is never written to disk; only the transcribed sentence goes
  on, as the task text -- exactly as if you'd typed it.
- Replies are spoken with Windows' own speech (pyttsx3 / SAPI5).

Optional dependencies, only needed for this file (see requirements.txt):
    pip install faster-whisper sounddevice pyttsx3
The first run downloads the Whisper model (VOICE_WHISPER_MODEL, ~150 MB for
base.en) from Hugging Face once; after that it works offline.

The hardware-facing pieces (Recorder, Transcriber, Speaker, the Windows
key polling) are small classes kept apart from the logic (is_yes,
make_voice_confirm, VoiceAssistant, PushToTalk), so tests/test_voice.py drives the logic
with fakes -- no microphone, no model, no network.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import os
import warnings
import queue
import re
import sys
import threading
import time
from typing import Callable

SAMPLE_RATE = 16000
MIN_SECONDS = 0.3  # shorter than this is a key tap, not speech
CONFIRM_LISTEN_SECONDS = 5.0  # how long to wait for an answer to START
# Tap-to-talk: recording ends after this much quiet following speech, and is
# dropped if no speech starts within WAIT_FOR_SPEECH_SECONDS.
SILENCE_TO_END_SECONDS = 1.0
WAIT_FOR_SPEECH_SECONDS = 6.0
MAX_RECORDING_SECONDS = 20.0
# A chunk counts as speech above this peak level, or 3x the quietest chunk
# heard in a noisy room -- that part capped at NOISY_ROOM_LEVEL, so talking
# from the very first chunk (quietest = loud) can't read as background.
# Silence on the user's microphone measured ~0.005.
MIN_SPEECH_LEVEL = 0.02
NOISY_ROOM_LEVEL = 0.1
_EPS = 1e-6  # chunk durations add up in floating point
MAX_SPOKEN_CHARS = 220  # about 15 seconds of speech
# Appended to a spoken task so Claude's finish summary is one sentence fit to
# be read aloud -- the first voice runs read out whole technical summaries
# ('... (Expression: "3 + 2=", Display: "5").'). The full task text, hint
# included, is what the log and output record show.
SPOKEN_TASK_HINT = (
    "\n\n(This request was spoken. When you finish, make the summary ONE short, plain sentence that "
    "can be read aloud, e.g. \"3 plus 2 is 5.\" -- no quotes, brackets or technical details.)"
)

YES_WORDS = frozenset({"yes", "confirm"})


# ---------------------------------------------------------------------------
# Logic (tested offline)
# ---------------------------------------------------------------------------

def is_yes(transcript: str) -> bool:
    """Only a plain "yes" or "confirm" counts -- the spoken version of the
    terminal's [y/n]. Whisper adds capitals and punctuation ("Yes."), which
    are ignored; anything longer ("yes, but first...", "yes please do the
    other one") is NOT a yes, because a risky action should never proceed on
    an ambiguous answer."""
    words = re.sub(r"[^a-z ]", " ", (transcript or "").lower()).split()
    return len(words) == 1 and words[0] in YES_WORDS


def short_for_speech(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """The result as it will be read aloud: agent failure messages are
    structured (WHAT HAPPENED / WHY / WHAT YOU CAN DO) -- speak only the
    first part; long summaries are cut at a sentence boundary."""
    text = (text or "").strip()
    if text.startswith("WHAT HAPPENED:"):
        text = text.split("\n", 1)[0].removeprefix("WHAT HAPPENED:").strip()
    first = re.match(r"(.+?[.!?])(?:\s|$)", text)
    if first and len(first.group(1)) >= 20:
        text = first.group(1)  # the first sentence carries the answer; details go to the screen, not the ear
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > limit // 3 else cut.rstrip() + "...")


def _loudness(audio) -> str:
    try:
        return f"{float(abs(audio).max()):.3f}; near 0.000 means the microphone picked up nothing"
    except Exception:  # noqa: BLE001 -- only a hint for the log
        return "unknown"


CLICK_WAIT_SECONDS = 30.0  # with the window: how long Yes/No stays up after no spoken answer


def make_voice_confirm(
    listen: Callable[..., object], transcribe: Callable[[object], str], say: Callable[[str], None],
    log: Callable[[str], None] = print, seconds: float = CONFIRM_LISTEN_SECONDS, ui=None,
    click_wait: float = CLICK_WAIT_SECONDS,
) -> Callable[[str], bool]:
    """A run_task() confirm_callback that asks aloud and listens for the
    answer -- and, with the window (voice_ui.Overlay), also shows Yes / No
    buttons; whichever answer comes first counts. Silence gets one second
    chance, then the buttons stay up for `click_wait` seconds. Only a plain
    spoken yes or a click on Yes continues; anything else, silence, or any
    failure to hear declines. `listen(seconds, should_abort)` records."""

    def hear(choice) -> str | None:
        """What was said, "" for nothing (or a click came first), or None if
        the microphone/model failed."""
        log("  [voice] listening for your yes or no (no key needed)...")
        abort = (lambda: choice.decided) if choice is not None else (lambda: False)
        try:
            audio = listen(seconds, abort)
            if abort():
                return ""  # answered by a click meanwhile
            heard = transcribe(audio)
        except Exception as e:  # a microphone or model error must never mean "yes"
            log(f"  [voice] could not hear an answer ({type(e).__name__}); treating it as no.")
            return None
        if not heard.strip():
            log(f"  [voice] heard nothing (loudest sample {_loudness(audio)})")
        return heard

    def clicked(choice) -> bool:
        log(f"  [voice] clicked {'Yes' if choice.value else 'No'}")
        return bool(choice.value)

    def confirm(prompt: str) -> bool:
        choice = ui.ask(prompt) if ui is not None else None
        ask = "Say yes or no" + (", or click a button." if choice is not None else ".")
        try:
            say(f"{prompt} {ask}")
            if choice is not None and choice.decided:
                return clicked(choice)
            heard = hear(choice)
            if heard is not None and not heard.strip() and not (choice and choice.decided):
                # Silence is not a "no" -- the first voice search stopped here
                # although the user never said no. Ask once more.
                say(f"I didn't hear an answer. {ask}")
                heard = hear(choice)
            if choice is not None and choice.decided:
                return clicked(choice)
            if heard:
                answer = is_yes(heard)
                log(f"  [voice] heard {heard!r} -> {'yes' if answer else 'no'}")
                return answer
            if choice is not None and heard is not None:
                log(f"  [voice] waiting up to {click_wait:.0f} s for a click on Yes or No...")
                if choice.wait(click_wait) is not None:
                    return clicked(choice)
            log("  [voice] no answer -> no")
            return False
        finally:
            if choice is not None:
                choice.answer(False)  # closed: a late click can't count (no-op if answered)
            if ui is not None:
                ui.end_question()

    return confirm


def submit_typed(text: str, state: dict, jobs: "queue.Queue") -> bool:
    """Queue a typed task unless one is already running or waiting (then
    False: the text stays in the box). Refusing rather than queueing also
    means the agent can't line up a task of its own by typing into this
    window while it runs -- the window's box is visible to its Windows arm."""
    text = text.strip()
    if not text:
        return False
    if state["busy"]:
        return False
    state["busy"] = True
    jobs.put(("text", text))
    return True


class TaskControl:
    """Stop / Pause / Take over / messages for the task that's running, shared
    by the stop key, the app's buttons and its message box. run_task() gets
    should_stop (which, while paused, simply waits -- the loop checks it
    before every step and every action, so pausing holds the agent between
    steps) and take_notes (messages typed mid-task)."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._running = threading.Event()  # set = not paused
        self._running.set()
        self._lock = threading.Lock()
        self._notes: list[str] = []

    def reset(self) -> None:
        with self._lock:
            self._notes.clear()
        self._stop.clear()
        self._running.set()

    def stop(self) -> None:
        self._stop.set()
        self._running.set()  # a paused task wakes up to stop

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def should_stop(self) -> bool:
        while not self._running.wait(0.2):
            if self._stop.is_set():
                break
        return self._stop.is_set()

    def add_note(self, text: str) -> None:
        text = text.strip()
        if text:
            with self._lock:
                self._notes.append(text)

    def take_notes(self) -> list[str]:
        with self._lock:
            notes, self._notes = self._notes, []
        return notes


QUESTION_WAIT_SECONDS = 120.0  # how long a question from the agent waits for a typed answer
HANDOFF_WAIT_SECONDS = 600.0   # how long a login wall waits for you to press Continue


def result_for_window(outcome: dict) -> str:
    """The result as the window shows it: the one-sentence summary, or for a
    failure what happened and why (not the long "what you can do")."""
    text = outcome.get("result", "") or ("Done." if outcome.get("success") else "That failed.")
    if outcome.get("success"):
        return short_for_speech(text) or text[:MAX_SPOKEN_CHARS]
    lines = [line.split(":", 1)[1].strip() if line.startswith(("WHAT HAPPENED:", "WHY:")) else line
             for line in text.splitlines() if not line.startswith("WHAT YOU CAN DO:")]
    return " ".join(line for line in lines if line)[:400]


class VoiceAssistant:
    """One spoken task at a time: transcribe -> run_task -> speak the result.
    `run` is agent.run_task (injected so tests can fake it)."""

    def __init__(self, config, transcribe, say, listen, run, log: Callable[[str], None] = print, quick=None,
                 ui=None):
        from voice_ui import NullUi

        self.config = config
        self.ui = ui or NullUi()  # voice_ui.Overlay: the floating window
        self.quick = quick  # quick_commands.QuickCommands, or None to always run the full agent
        self.transcribe = transcribe
        self.say = say
        self.log = log
        self.run = run
        self.control = TaskControl()
        # Set while a confirmation question is listening for yes/no, so the
        # key loop can say "no key needed" instead of "still working".
        self.answering = threading.Event()

        def listen_for_answer(seconds: float, should_abort=None):
            self.answering.set()
            try:
                return listen(seconds, should_abort)
            finally:
                self.answering.clear()

        self.confirm = make_voice_confirm(listen_for_answer, transcribe, say, log, ui=ui)
        self._listen_for_answer = listen_for_answer

    def handle_audio(self, audio) -> dict | None:
        """Returns run_task's outcome, or None if nothing usable was said."""
        self.ui.status("working", "Got it...")
        text = (self.transcribe(audio) or "").strip()
        if len(text) < 3:
            self.log("  [voice] didn't catch anything.")
            self.ui.status("ready", "Didn't catch that -- try again")
            return None
        self.log(f"\nYou said: {text}")
        return self.handle_text(text)

    def handle_job(self, job: tuple[str, object]) -> dict | None:
        """One queued job: ("audio", recording) or ("text", typed task)."""
        kind, payload = job
        if kind == "text":
            self.log(f"\nYou typed: {payload}")
            return self.handle_text(str(payload), source="typed")
        return self.handle_audio(payload)

    def handle_text(self, text: str, source: str = "voice") -> dict | None:
        """A task as text -- spoken (after transcribing) or typed. Same path
        either way: quick command if it is one, else the full agent."""
        text = text.strip()
        if not text:
            return None
        self.ui.status("working", "Got it...")
        self.ui.task(text, source)
        if self.quick is not None:
            try:
                reply = self.quick.try_handle(text)
            except Exception as e:  # a failed shortcut falls back to the full agent, never to silence
                self.log(f"  [quick] failed ({e}); running the full agent instead.")
                reply = None
            if reply:
                self.ui.result(reply, True)
                self.say(reply)
                return {"success": True, "result": reply, "quick": True}
        self.ui.status("working", "Working on it...")
        self.ui.step("Thinking about the first step...")
        self.say("On it.")
        self.control.reset()
        outcome = self.run(
            text + SPOKEN_TASK_HINT, self.config, confirm_callback=self.confirm, should_stop=self.control.should_stop,
            on_step=self.ui.agent_step, task_updates=self.control.take_notes, ask_user=self.ask_user,
            handoff=self.handoff,
        )
        self.control.resume()
        if not outcome.get("success"):
            # Speak the headline, but show the whole explanation (WHY / WHAT YOU
            # CAN DO) on screen -- the first failure seen in voice mode only said
            # "could not be reached", hiding the actual API error.
            self.log(f"  [voice] task failed:\n{outcome.get('result', '')}")
        self.ui.result(result_for_window(outcome), bool(outcome.get("success")))
        self.say(short_for_speech(outcome.get("result", "")) or ("Done." if outcome.get("success") else "That failed."))
        return outcome

    def request_stop(self) -> None:
        self.log("  [voice] stop requested -- the task will stop before its next action.")
        self.ui.info("Stopping before the next step...")
        self.control.stop()

    def ask_user(self, question: str) -> str | None:
        """The agent's ask_user tool: ask aloud (and in the app, with a box to
        type into); the first answer -- spoken or typed -- counts."""
        choice = self.ui.ask_text(question)
        self.log(f"  [agent asks] {question}")
        try:
            self.say(question)
            if choice is not None and choice.decided:
                return str(choice.value or "")
            try:
                audio = self._listen_for_answer(8.0, (lambda: choice.decided) if choice is not None else None)
                heard = "" if (choice is not None and choice.decided) else (self.transcribe(audio) or "").strip()
            except Exception as e:  # noqa: BLE001 -- no microphone: typing still works
                self.log(f"  [voice] could not listen for the answer ({type(e).__name__})")
                heard = ""
            if choice is not None and choice.decided:
                return str(choice.value or "")
            if heard:
                self.log(f"  [voice] answer: {heard!r}")
                return heard
            if choice is not None:
                value = choice.wait(QUESTION_WAIT_SECONDS)
                return str(value) if value else None
            return None
        finally:
            if choice is not None:
                choice.answer("")  # closed: a late answer can't count
            self.ui.end_question()

    def handoff(self, message: str) -> bool:
        """A login/CAPTCHA wall: your turn. Only the app has a Continue button;
        without it the task stops, as it always has with no terminal."""
        choice = self.ui.handoff(message)
        if choice is None:
            return False
        self.say("I need you to log in or verify in the browser. Press Continue when you're done.")
        try:
            value = choice.wait(HANDOFF_WAIT_SECONDS)
            return value is True
        finally:
            choice.answer(False)
            self.ui.end_question()


# ---------------------------------------------------------------------------
# Hardware (Windows; not exercised by tests/)
# ---------------------------------------------------------------------------

class Recorder:
    """Microphone via sounddevice: hold-to-record, or record for N seconds."""

    def __init__(self):
        import numpy as np
        import sounddevice as sd

        self._np, self._sd = np, sd
        self._chunks: list = []
        self._stream = None

    def start(self, detector: SpeechEndDetector | None = None) -> None:
        self._chunks = []

        def on_audio(data, frames, t, status):
            self._chunks.append(data.copy())
            if detector is not None:
                detector.add(float(self._np.abs(data).max()) if len(data) else 0.0, frames / SAMPLE_RATE)

        self._stream = self._sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=on_audio)
        self._stream.start()

    def record_until_quiet(self, wait_for_speech: float, should_abort: Callable[[], bool] | None = None):
        """Record until the speaker stops (see SpeechEndDetector), waiting up
        to `wait_for_speech` seconds for them to begin -- a spoken "yes" is
        sent about a second after it's said, not after a fixed window."""
        detector = SpeechEndDetector(wait_for_speech=wait_for_speech)
        self.start(detector)
        try:
            while not detector.done and not (should_abort and should_abort()):
                time.sleep(0.05)
        finally:
            audio = self.stop()
        return audio

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if not self._chunks:
            return self._np.zeros(0, dtype="float32")
        return self._np.concatenate(self._chunks)[:, 0]

    def record_for(self, seconds: float):
        audio = self._sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        self._sd.wait()
        return audio[:, 0]


class Transcriber:
    """Local speech-to-text (faster-whisper). Audio never leaves the machine."""

    def __init__(self, model_name: str, language: str):
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model_name, device="auto", compute_type="int8")
        self._language = language or None

    def __call__(self, audio) -> str:
        if audio is None or len(audio) < SAMPLE_RATE * MIN_SECONDS:
            return ""
        segments, _ = self._model.transcribe(audio, language=self._language, beam_size=1, vad_filter=True)
        return " ".join(s.text for s in segments).strip()


class Speaker:
    """Windows speech (pyttsx3/SAPI5). Called only from the worker thread,
    since the SAPI engine doesn't like being used across threads."""

    def __init__(self):
        import pyttsx3

        self._engine = pyttsx3.init()

    def __call__(self, text: str) -> None:
        print(f"  [says] {text}")
        self._engine.say(text)
        self._engine.runAndWait()


# Windows virtual-key codes for the names VOICE_PTT_KEY / VOICE_STOP_KEY
# accept. Keys are read by polling GetAsyncKeyState (~50x a second), not by
# a keyboard hook: on the first real try (2026-09-24) pynput's hook received
# no key presses at all on the user's PC, while polling needs no hook, no
# extra package and no admin rights.
VK_CODES = {
    "ctrl_r": 0xA3, "ctrl_l": 0xA2, "ctrl": 0x11,
    "alt_r": 0xA5, "alt_gr": 0xA5, "alt_l": 0xA4,
    "shift_r": 0xA1, "shift_l": 0xA0,
    "caps_lock": 0x14, "scroll_lock": 0x91, "pause": 0x13, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22, "menu": 0x5D,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},  # f1 = 0x70 ... f24 = 0x87
}


def key_code(name: str) -> int:
    """VK code for a key name (see VK_CODES), or a single letter/digit."""
    name = name.strip().lower()
    if name in VK_CODES:
        return VK_CODES[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    raise ValueError(
        f"Unknown key name {name!r} -- use e.g. ctrl_r, f9, f10, alt_gr, scroll_lock, or a single letter/digit."
    )


class PushToTalk:
    """Turns "is the key down right now?" samples into events: 'start' when
    the talk key goes down, 'send' when it comes back up, 'stop' when the
    stop key goes down. Pure logic, so tests/ can drive it without a
    keyboard; the Windows polling loop in main() just feeds it samples."""

    def __init__(self):
        self._talk_was_down = False
        self._stop_was_down = False

    def update(self, talk_down: bool, stop_down: bool) -> list[str]:
        events = []
        if talk_down and not self._talk_was_down:
            events.append("start")
        elif not talk_down and self._talk_was_down:
            events.append("send")
        if stop_down and not self._stop_was_down:
            events.append("stop")
        self._talk_was_down, self._stop_was_down = talk_down, stop_down
        return events


class SpeechEndDetector:
    """Decides, from each audio chunk's peak level, when a tap-to-talk
    recording is over: SILENCE_TO_END_SECONDS of quiet after some speech,
    no speech at all within WAIT_FOR_SPEECH_SECONDS, or MAX_RECORDING_SECONDS.
    Pure logic; Recorder feeds it from the microphone callback."""

    def __init__(self, silence_to_end: float = SILENCE_TO_END_SECONDS,
                 wait_for_speech: float = WAIT_FOR_SPEECH_SECONDS, max_seconds: float = MAX_RECORDING_SECONDS,
                 min_level: float = MIN_SPEECH_LEVEL):
        self.silence_to_end, self.wait_for_speech = silence_to_end, wait_for_speech
        self.max_seconds, self.min_level = max_seconds, min_level
        self.elapsed = 0.0
        self.quiet_for = 0.0
        self.heard_speech = False
        self._floor: float | None = None

    def add(self, peak: float, seconds: float) -> None:
        self.elapsed += seconds
        self._floor = peak if self._floor is None else min(self._floor, peak)
        if peak >= max(self.min_level, min(3 * self._floor, NOISY_ROOM_LEVEL)):
            self.heard_speech, self.quiet_for = True, 0.0
        else:
            self.quiet_for += seconds

    @property
    def done(self) -> bool:
        if self.elapsed >= self.max_seconds - _EPS:
            return True
        if self.heard_speech:
            return self.quiet_for >= self.silence_to_end - _EPS
        return self.elapsed >= self.wait_for_speech - _EPS


def talk_key_action(mode: str, event: str, recording: bool) -> str | None:
    """What a talk-key event means: 'start' or 'send' a recording, or None.
    tap: each press starts, or sends early if already recording (release is
    ignored; the recording normally ends by itself). hold: press starts,
    release sends -- the original push-to-talk."""
    if mode == "hold":
        return {"start": "start", "send": "send"}.get(event)
    if event == "start":
        return "send" if recording else "start"
    return None


# --- run without a terminal ---------------------------------------------------

AUTOSTART_NAME = "AI Agent voice.cmd"


def startup_folder() -> str:
    """Windows' per-user Startup folder (what Win+R shell:startup opens)."""
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def set_autostart(on: bool, startup_dir: str, project_dir: str) -> str:
    """Add or remove a small launcher in the Startup folder that runs
    start_voice.bat, minimized, at every login. Returns what happened."""
    path = os.path.join(startup_dir, AUTOSTART_NAME)
    if not on:
        if os.path.exists(path):
            os.remove(path)
            return f"Removed {path} -- the voice assistant no longer starts at login."
        return "It wasn't set to start at login; nothing to remove."
    os.makedirs(startup_dir, exist_ok=True)
    bat = os.path.join(project_dir, "start_voice.bat")
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(f'@echo off\nstart "AI Agent voice" /min "{bat}"\n')
    return f"Created {path} -- the voice assistant will start (minimized) whenever you log in."


_instance_lock = None  # keeps the single-instance mutex alive while running


def claim_single_instance() -> bool:
    """False if another voice assistant is already running (Windows named
    mutex): two copies would both react to the same key and run every task
    twice -- easy to cause once it also starts at login."""
    global _instance_lock
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _instance_lock = kernel32.CreateMutexW(None, False, "Local\\AIBrowserAgentVoice")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def _key_is_down():
    """Returns is_down(vk) -> bool, reading the key's live state from Windows."""
    if sys.platform != "win32":
        raise OSError("voice.py's push-to-talk key reading is Windows-only.")
    import ctypes

    get_state = ctypes.windll.user32.GetAsyncKeyState
    return lambda vk: bool(get_state(vk) & 0x8000)


def key_test(seconds: float = 20.0) -> None:
    """Print the name of every supported key as it's pressed -- the names
    VOICE_PTT_KEY / VOICE_STOP_KEY accept."""
    is_down = _key_is_down()
    names = {**{k: v for k, v in VK_CODES.items() if k not in ("alt_gr", "ctrl")},
             **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"}}
    print(f"Press some keys (including the one you want for talking). Showing them for {seconds:.0f} s...")
    was_down: set[str] = set()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        down = {name for name, vk in names.items() if is_down(vk)}
        for name in sorted(down - was_down):
            print(f"  pressed: {name}")
        was_down = down
        time.sleep(0.02)
    print("Done. Use a name shown above as VOICE_PTT_KEY in .env (e.g. VOICE_PTT_KEY=f9).")


def mic_test(config, seconds: float = 4.0) -> None:
    """Record a few seconds (no key needed) and print the transcript --
    checks the microphone and the speech model on their own."""
    import numpy as np

    recorder = Recorder()
    transcriber = Transcriber(config.voice_whisper_model, config.voice_language)
    print(f"Recording for {seconds:.0f} s -- say something now...")
    audio = recorder.record_for(seconds)
    level = float(np.abs(audio).max()) if len(audio) else 0.0
    print(f"  loudest sample: {level:.3f}  (near 0.000 means the microphone isn't picking anything up)")
    print(f"  heard: {transcriber(audio)!r}")


def build_quick_commands(config):
    """QuickCommands wired to the real Windows actions, or None when turned
    off (VOICE_QUICK_COMMANDS=false). App launches go through the Windows
    arm's own risk check and launcher, so they follow exactly the SAFE_APPS
    rule the agent uses; if that arm can't load, apps go to the full agent."""
    if not config.voice_quick_commands:
        return None
    import webbrowser

    from quick_commands import QuickCommands, windows_media_key

    try:
        from windows_tools import WindowsSession, WindowsToolProvider, normalize_app_name

        safe_apps = frozenset(normalize_app_name(a) for a in config.safe_apps.split(",") if a.strip())

        provider = WindowsToolProvider(WindowsSession(), safe_apps=safe_apps)
        is_safe = lambda exe: provider.get_dynamic_risk("windows_launch_app", {"path": exe}) == "R0"  # noqa: E731
        launch = lambda exe: provider.session.execute("windows_launch_app", {"path": exe})  # noqa: E731
        screenshot = lambda: provider.session.execute("windows_screenshot", {})  # noqa: E731
    except Exception:  # noqa: BLE001 -- no Windows arm here: never launch via the shortcut
        safe_apps = frozenset()
        is_safe, launch, screenshot = (lambda exe: False), (lambda exe: None), None
    jev = None
    if config.typesafe_api_key:
        from jev import shared_client

        jev = shared_client(config.typesafe_api_key, config.typesafe_model)
    return QuickCommands(
        safe_apps, launch_app=launch, open_url=webbrowser.open, press_media_key=windows_media_key,
        jev=jev, min_confidence=config.quick_min_confidence, is_safe_launch=is_safe,
        take_screenshot=None if config.confirm_r1_actions else screenshot,  # R1: asks when that's on
    )


LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "voice.log")
_no_console = False  # started by start_voice.bat via pythonw.exe: no terminal window


def _alert(message: str) -> None:
    """Print, and with no terminal window also show a message box -- or a
    setup problem would fail silently when started from start_voice.bat."""
    print(message)
    if _no_console:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("AI Agent voice", message)
            root.destroy()
        except Exception:  # noqa: BLE001 -- the log file still has it
            pass


def main() -> None:
    global _no_console
    parser = argparse.ArgumentParser(description="Voice front-end for the agent.")
    parser.add_argument("--keys", action="store_true", help="show which keys the program sees, then exit")
    parser.add_argument("--mic-test", action="store_true", help="record 4 s, show what was heard, then exit")
    parser.add_argument("--autostart", choices=["on", "off"],
                        help="start the voice assistant whenever you log in to Windows (on), or stop that (off)")
    args = parser.parse_args()
    if sys.stdout is None or sys.stderr is None:
        # pythonw.exe (start_voice.bat): no terminal, so everything that would
        # have been printed goes to output/voice.log (tray icon > Open log).
        _no_console = True
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        sys.stdout = sys.stderr = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        print(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    # The speech-model download's symlink warning is harmless on Windows (it
    # just uses a bit more disk); keep the startup output readable.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    # pywinauto notes it switched COM to single-threaded mode after the audio
    # libraries loaded first; it works either way, so don't show it every run.
    warnings.filterwarnings("ignore", message="Revert to STA COM threading mode")

    if args.autostart:
        print(set_autostart(args.autostart == "on", startup_folder(), os.path.dirname(os.path.abspath(__file__))))
        return
    try:
        if args.keys:
            try:
                key_test()
            except OSError as e:
                print(e)
                sys.exit(1)
            return
        from config import load_config

        config = load_config()
        if args.mic_test:
            mic_test(config)
            return
        from agent import run_task
    except ModuleNotFoundError as e:
        _alert(f"Missing package '{e.name}'. Is the project's environment active? Your prompt should start with "
               "(.venv) -- if not, run: .venv\\Scripts\\activate\n"
               "Voice also needs: pip install faster-whisper sounddevice pyttsx3 pystray pillow")
        sys.exit(1)

    problems = config.validate()
    if problems:
        _alert("Configuration problem(s) found:\n" + "\n".join(f"  - {p}" for p in problems))
        sys.exit(1)
    if _no_console and not config.voice_ui:
        _alert("VOICE_UI=false needs a terminal: run 'python voice.py' there, or set VOICE_UI=true in .env.")
        sys.exit(1)
    if not claim_single_instance():
        _alert("The voice assistant is already running (see the icon by the clock). Not starting a second one.")
        sys.exit(1)
    mode = config.voice_mode
    from voice_ui import Tray

    try:
        ptt_vk, stop_vk = key_code(config.voice_ptt_key), key_code(config.voice_stop_key)
        is_down = _key_is_down()
        recorder = Recorder()
        print(f"Loading the speech model '{config.voice_whisper_model}' (first run downloads it once)...")
        transcriber = Transcriber(config.voice_whisper_model, config.voice_language)
    except ImportError as e:
        _alert(f"Voice needs extra packages: pip install faster-whisper sounddevice pyttsx3  ({e})")
        sys.exit(1)
    except (ValueError, OSError) as e:
        _alert(f"Voice setup problem: {e}")
        sys.exit(1)

    verb = "Hold" if mode == "hold" else "Tap"
    hint = f"{verb} {config.voice_ptt_key} and speak, or type below. {config.voice_stop_key} stops a task."
    jobs: queue.Queue = queue.Queue()
    state = {"recording": False, "busy": False, "detector": None, "paused": False}

    def on_typed(text: str) -> bool:
        if submit_typed(text, state, jobs):
            return True
        if ui is not None:
            ui.status("working", f"Still working on the last task -- {config.voice_stop_key} stops it")
        print("  [voice] still working on the last task -- press the stop key to cancel it.")
        return False

    ui, root, tray, app = None, None, None, None
    if config.voice_ui:
        # The full app window (app_ui.py, pywebview) when it's installed;
        # otherwise the small tkinter window below.
        try:
            from app_ui import App

            app = App(
                is_busy=lambda: state["busy"], start_task=lambda text: submit_typed(text, state, jobs),
                control=lambda: holder["assistant"].control, on_mic=lambda: toggle_mic(),
                output_dir=Path(LOG_PATH).parent,
                info={"talk_key": config.voice_ptt_key, "stop_key": config.voice_stop_key, "voice_mode": mode,
                      "provider": config.llm_provider, "model": config.llm_model, "decider": config.decider},
            )
            ui = app.state
        except ImportError as e:
            print(f"  [voice] the full app window needs pywebview (pip install pywebview) -- {e}; "
                  "using the small window instead.")
        except Exception as e:  # noqa: BLE001 -- e.g. WebView2 missing: the small window still works
            print(f"  [voice] could not open the app window ({e}); using the small window instead.")
            app = None
    if config.voice_ui and app is None:
        try:
            import tkinter as tk

            from voice_ui import Overlay

            root = tk.Tk()
            ui = Overlay(root, hint, on_status=lambda kind: tray and tray.set_status(kind), on_text=on_typed)
        except Exception as e:  # noqa: BLE001 -- e.g. tkinter missing: fall back to the terminal
            if _no_console:
                _alert(f"Could not open the voice window ({e}). Run 'python voice.py' in a terminal instead.")
                sys.exit(1)
            print(f"  [voice] no window ({e}); using this terminal instead.")
            ui, root = None, None

    ready = threading.Event()
    holder: dict = {}

    def worker() -> None:
        speaker = Speaker()  # created on this thread; only ever used from it
        holder["assistant"] = VoiceAssistant(
            config, transcriber, speaker, recorder.record_until_quiet, run_task, quick=build_quick_commands(config),
            ui=ui,
        )
        ready.set()
        if ui is not None:
            ui.status("ready", f"Ready -- {verb.lower()} {config.voice_ptt_key}")
        speaker(f"Ready. {verb} {config.voice_ptt_key} and speak.")
        while True:
            job = jobs.get()
            state["busy"] = True
            try:
                holder["assistant"].handle_job(job)
            except Exception as e:  # one bad task must not end the voice loop
                print(f"  [voice] error: {e}")
                if ui is not None:
                    ui.result(f"Something went wrong: {e}", False)
                speaker("Something went wrong with that one.")
            finally:
                state["busy"] = False

    threading.Thread(target=worker, daemon=True).start()
    ready.wait()

    def show_status(kind: str, text: str) -> None:
        print(f"  [voice] {text}")
        if ui is not None:
            ui.status(kind, text)

    def start_recording() -> None:
        if ui is not None:
            ui.show()
        if state["paused"]:
            show_status("paused", "Microphone paused -- resume it from the icon by the clock")
            return
        if state["busy"]:
            if holder["assistant"].answering.is_set():
                print("  [voice] (no need to press the key for yes/no -- just say it)")
            else:
                print("  [voice] still working on the last task -- press the stop key to cancel it.")
            return
        state["detector"] = SpeechEndDetector() if mode == "tap" else None
        try:
            recorder.start(state["detector"])
        except Exception as e:  # e.g. no microphone, or it's in use -- say so instead of failing silently
            show_status("failed", f"Could not start the microphone: {e}")
            return
        state["recording"] = True
        show_status("listening", "Listening..." + (" (stops when you stop talking)" if mode == "tap" else ""))

    def send_recording() -> None:
        if not state["recording"]:
            return
        state["recording"] = False
        audio = recorder.stop()
        detector = state["detector"]
        if detector is not None and not detector.heard_speech:
            show_status("ready", f"Didn't hear anything -- {verb.lower()} and speak again")
            return
        print(f"  [voice] got {len(audio) / SAMPLE_RATE:.1f} s of audio, transcribing...")
        state["busy"] = True
        jobs.put(("audio", audio))

    def toggle_mic() -> None:
        """The app's microphone button: the same as tapping the talk key."""
        if state["recording"]:
            send_recording()
        else:
            start_recording()

    ptt = PushToTalk()

    def tick() -> None:
        for event in ptt.update(is_down(ptt_vk), is_down(stop_vk)):
            action = talk_key_action(mode, event, state["recording"])
            if action == "start":
                start_recording()
            elif action == "send":
                send_recording()
            elif event == "stop" and state["busy"]:
                holder["assistant"].request_stop()
        if state["recording"] and state["detector"] is not None and state["detector"].done:
            send_recording()  # tap mode: you stopped talking

    print(f"{verb} {config.voice_ptt_key} to talk, {config.voice_stop_key} to stop a task, "
          + ("Quit from the icon by the clock." if ui is not None else "Ctrl+C here (or close this window) to quit."))
    print("  (Nothing happens when you press the key? Run: python voice.py --keys)")
    if not _no_console and sys.stdin is not None and sys.stdin.isatty():
        print("  You can also type a task here and press Enter.")

        def read_typed() -> None:
            for line in sys.stdin:
                if line.strip():
                    on_typed(line)

        threading.Thread(target=read_typed, daemon=True).start()

    def set_paused(paused: bool) -> None:
        state["paused"] = paused
        if paused and state["recording"]:
            state["recording"] = False
            recorder.stop()  # drop it: pausing means stop listening now
        if paused:
            show_status("paused", "Microphone paused")
        else:
            show_status("ready", f"Ready -- {verb.lower()} {config.voice_ptt_key}")

    if app is not None:
        from app_ui import run_app

        tray = Tray.start(on_show=lambda: app.set_mode(app.mode), on_pause=set_paused, on_quit=app.quit,
                          log_path=LOG_PATH)
        app.tray = tray
        if tray is None:
            print("  [voice] no icon by the clock (pip install pystray pillow); closing the window quits.")
        run_app(app, tick)
        if tray is not None:
            tray.stop()
        print("Bye.")
        os._exit(0)

    if ui is not None:
        tray = Tray.start(on_show=ui.show, on_pause=set_paused, on_quit=ui.quit, log_path=LOG_PATH)
        if tray is None:
            print("  [voice] no icon by the clock (pip install pystray pillow); the window's '-' hides it, "
                  "and the talk key brings it back.")

        def poll() -> None:
            tick()
            root.after(20, poll)

        root.after(20, poll)
        try:
            root.mainloop()
        except KeyboardInterrupt:
            pass
        if tray is not None:
            tray.stop()
        print("Bye.")
        os._exit(0)  # the worker may be mid-task or mid-sentence; quitting means now

    try:
        while True:
            tick()
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
