#!/usr/bin/env python3
"""
Voice front-end: hold a key, say a task, the agent does it and says the result.

    python voice.py
    python voice.py --keys       # troubleshooting: show which keys this program sees
    python voice.py --mic-test   # troubleshooting: record 4 s and show what was heard

Hold VOICE_PTT_KEY (default: right Ctrl) while speaking and release to send.
Press VOICE_STOP_KEY (default: F10) to stop a running task between steps.
Ctrl+C in this window quits.

Like discord_bot.py, this is only another way to call run_task(): the loop,
the arms, the risk tiers and every confirmation are the same ones
`python agent.py` uses. What's different is *how you're asked*: a
confirmation is spoken aloud and answered by voice, and only a plain "yes"
or "confirm" continues -- silence, anything else, or anything that can't be
heard declines (fail-closed, same as a typed "n").

Privacy by design (docs/JEV_VOICE_PLAN.md Phase 3):
- Push-to-talk: the microphone records only while the key is held, and for a
  few seconds after a spoken confirmation question. Nothing is always-on.
- Speech-to-text runs locally (faster-whisper). Audio never leaves this
  machine and is never written to disk; only the transcribed sentence goes
  on, as the task text -- exactly as if you'd typed it.
- Replies are spoken with Windows' own speech (pyttsx3 / SAPI5).

Optional dependencies, only needed for this file (see requirements.txt):
    pip install faster-whisper sounddevice pynput pyttsx3
The first run downloads the Whisper model (VOICE_WHISPER_MODEL, ~150 MB for
base.en) from Hugging Face once; after that it works offline.

The hardware-facing pieces (Recorder, Transcriber, Speaker, the hotkey
listener) are small classes kept apart from the logic (is_yes,
make_voice_confirm, VoiceAssistant), so tests/test_voice.py drives the logic
with fakes -- no microphone, no model, no network.
"""
from __future__ import annotations

import argparse
import os
import queue
import re
import sys
import threading
from typing import Callable

SAMPLE_RATE = 16000
MIN_SECONDS = 0.3  # shorter than this is a key tap, not speech
CONFIRM_LISTEN_SECONDS = 4.0
MAX_SPOKEN_CHARS = 400

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
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > limit // 3 else cut.rstrip() + "...")


def make_voice_confirm(
    listen: Callable[[float], object], transcribe: Callable[[object], str], say: Callable[[str], None],
    log: Callable[[str], None] = print, seconds: float = CONFIRM_LISTEN_SECONDS,
) -> Callable[[str], bool]:
    """A run_task() confirm_callback that asks aloud and listens for the
    answer. Any failure to hear or understand declines."""

    def confirm(prompt: str) -> bool:
        say(f"{prompt} Say yes or no.")
        try:
            heard = transcribe(listen(seconds))
        except Exception as e:  # a microphone or model error must never mean "yes"
            log(f"  [voice] could not hear an answer ({type(e).__name__}); treating it as no.")
            return False
        answer = is_yes(heard)
        log(f"  [voice] heard {heard!r} -> {'yes' if answer else 'no'}")
        return answer

    return confirm


class VoiceAssistant:
    """One spoken task at a time: transcribe -> run_task -> speak the result.
    `run` is agent.run_task (injected so tests can fake it)."""

    def __init__(self, config, transcribe, say, listen, run, log: Callable[[str], None] = print):
        self.config = config
        self.transcribe = transcribe
        self.say = say
        self.log = log
        self.run = run
        self.stop_event = threading.Event()
        self.confirm = make_voice_confirm(listen, transcribe, say, log)

    def handle_audio(self, audio) -> dict | None:
        """Returns run_task's outcome, or None if nothing usable was said."""
        text = (self.transcribe(audio) or "").strip()
        if len(text) < 3:
            self.log("  [voice] didn't catch anything.")
            return None
        self.log(f"\nYou said: {text}")
        self.say("On it.")
        self.stop_event.clear()
        outcome = self.run(
            text, self.config, confirm_callback=self.confirm, should_stop=self.stop_event.is_set,
        )
        self.say(short_for_speech(outcome.get("result", "")) or ("Done." if outcome.get("success") else "That failed."))
        return outcome

    def request_stop(self) -> None:
        self.log("  [voice] stop requested -- the task will stop before its next action.")
        self.stop_event.set()


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

    def start(self) -> None:
        self._chunks = []
        self._stream = self._sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32",
            callback=lambda data, frames, t, status: self._chunks.append(data.copy()),
        )
        self._stream.start()

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


def _parse_key(name: str):
    from pynput import keyboard

    name = name.strip().lower()
    if hasattr(keyboard.Key, name):
        return getattr(keyboard.Key, name)
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    raise ValueError(f"Unknown key name {name!r} -- use e.g. ctrl_r, f9, f10, alt_gr, or a single character.")


def key_test(seconds: float = 20.0) -> None:
    """Print every key press exactly as the listener sees it -- the name to
    put in VOICE_PTT_KEY / VOICE_STOP_KEY. If nothing prints at all, the
    keyboard hook isn't receiving events (see README troubleshooting)."""
    from pynput import keyboard

    print(f"Press some keys (including the one you want for talking). Showing them for {seconds:.0f} s...")

    def show(key) -> None:
        name = key.name if isinstance(key, keyboard.Key) else getattr(key, "char", None) or repr(key)
        print(f"  pressed: {name}")

    with keyboard.Listener(on_press=show) as listener:
        listener.join(seconds)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Voice front-end for the agent.")
    parser.add_argument("--keys", action="store_true", help="show which keys the program sees, then exit")
    parser.add_argument("--mic-test", action="store_true", help="record 4 s, show what was heard, then exit")
    args = parser.parse_args()
    # The speech-model download's symlink warning is harmless on Windows (it
    # just uses a bit more disk); keep the startup output readable.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    if args.keys:
        key_test()
        return

    from agent import run_task
    from config import load_config

    config = load_config()
    if args.mic_test:
        mic_test(config)
        return

    problems = config.validate()
    if problems:
        print("Configuration problem(s) found:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    try:
        from pynput import keyboard

        ptt_key, stop_key = _parse_key(config.voice_ptt_key), _parse_key(config.voice_stop_key)
        recorder = Recorder()
        print(f"Loading the speech model '{config.voice_whisper_model}' (first run downloads it once)...")
        transcriber = Transcriber(config.voice_whisper_model, config.voice_language)
    except ImportError as e:
        print(f"Voice needs extra packages: pip install faster-whisper sounddevice pynput pyttsx3  ({e})")
        sys.exit(1)

    jobs: queue.Queue = queue.Queue()
    state = {"recording": False, "busy": False}
    ready = threading.Event()
    holder: dict = {}

    def worker() -> None:
        speaker = Speaker()  # created on this thread; only ever used from it
        holder["assistant"] = VoiceAssistant(
            config, transcriber, speaker, recorder.record_for, run_task,
        )
        ready.set()
        speaker(f"Ready. Hold {config.voice_ptt_key} and speak.")
        while True:
            audio = jobs.get()
            state["busy"] = True
            try:
                holder["assistant"].handle_audio(audio)
            except Exception as e:  # one bad task must not end the voice loop
                print(f"  [voice] error: {e}")
                speaker("Something went wrong with that one.")
            finally:
                state["busy"] = False

    threading.Thread(target=worker, daemon=True).start()
    ready.wait()

    def on_press(key) -> None:
        if key == stop_key and state["busy"]:
            holder["assistant"].request_stop()
        elif key == ptt_key and not state["recording"]:  # key repeat sends many presses; start once
            if state["busy"]:
                print("  [voice] still working on the last task -- press the stop key to cancel it.")
                return
            try:
                recorder.start()
            except Exception as e:  # e.g. no microphone, or it's in use -- say so instead of failing silently
                print(f"  [voice] could not start the microphone: {e}")
                return
            state["recording"] = True
            print("  [voice] listening...")

    def on_release(key) -> None:
        if key == ptt_key and state["recording"]:
            state["recording"] = False
            audio = recorder.stop()
            print(f"  [voice] got {len(audio) / SAMPLE_RATE:.1f} s of audio, transcribing...")
            jobs.put(audio)

    print(f"Hold {config.voice_ptt_key} to talk, {config.voice_stop_key} to stop a task, Ctrl+C here to quit.")
    print("  (Nothing happens when you hold the key? Run: python voice.py --keys)")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            print("\nBye.")


if __name__ == "__main__":
    main()
