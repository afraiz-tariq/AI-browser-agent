"""
Quick commands for voice: simple one-step requests ("open notepad", "volume
up", "pause", "go to youtube", "search youtube for lofi beats") done in well
under a second, without a full agent run.

Asked for after the first voice runs: "open notepad" took two Claude steps
(~5 s) through run_task(). The idea is Rocky's fast path
(docs/JEV_VOICE_PLAN.md), fitted to this project's rules:

1. An exact-phrase matcher runs first -- instant, offline, deterministic.
2. If that doesn't match and TYPESAFE_API_KEY is set, ONE Jev request picks
   the command and its argument from fixed lists (Jev never writes text),
   which covers paraphrases ("fire up the calculator", "turn it down").
3. Anything else -- more than one step, anything Jev isn't sure of
   (QUICK_MIN_CONFIDENCE), an app not on SAFE_APPS -- returns None and the
   caller runs the full agent exactly as before. Unsure means "full agent",
   never "do something anyway".

Safety, the same rules as the agent's arms:
- Apps open only through WindowsToolProvider's own risk check: a quick
  launch happens only if get_dynamic_risk() says R0, i.e. a SAFE_APPS app
  by bare name with no arguments. Anything else goes to the full agent,
  which asks [y/n].
- Web addresses are built by code from a fixed site list or a strictly
  validated spoken domain; search queries are URL-encoded spans of what you
  said. Opening a page is what the browser arm's `goto` does (R0).
- Volume and media keys are classified R0 here, explicitly (the "new
  actions start at R3 until classified in code" rule): they're reversible,
  touch no data, and are what the keyboard's own media keys do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus

from jev import JevError, validate_choice

# Spoken name -> executable. Only apps that are also on SAFE_APPS can open
# via a quick command; the rest go to the full agent (which asks).
APP_ALIASES = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "ms paint": "mspaint.exe",
    "snipping tool": "snippingtool.exe", "snip": "snippingtool.exe",
    "file explorer": "explorer.exe", "explorer": "explorer.exe", "files": "explorer.exe",
    "my files": "explorer.exe",
}

SITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "wikipedia": "https://en.wikipedia.org",
    "amazon": "https://www.amazon.com",
    "reddit": "https://www.reddit.com",
    "linkedin": "https://www.linkedin.com",
    "netflix": "https://www.netflix.com",
    "claude": "https://claude.ai",
}

SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={q}",
}

# Windows virtual-key codes for the keyboard's own media keys.
MEDIA_KEYS = {
    "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
    "play_pause": 0xB3, "next_track": 0xB0, "previous_track": 0xB1,
}
VOLUME_STEPS = 5  # each press is ~2% on most systems

REPLIES = {
    "volume_up": "Volume up.", "volume_down": "Volume down.", "mute": "Muted.",
    "play_pause": "Okay.", "next_track": "Next track.", "previous_track": "Previous track.",
}

_DOMAIN = re.compile(r"^(?:www\.)?([a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})*\.[a-z]{2,24})$")


@dataclass
class QuickCommand:
    kind: str       # open_app | open_url | key
    value: str      # exe name, URL, or MEDIA_KEYS name
    reply: str      # what gets said
    source: str     # "phrase" or "jev"


def _normalize(text: str) -> str:
    text = re.sub(r"[^a-z0-9.' -]", " ", (text or "").lower())
    text = " ".join(text.split()).strip(" .")
    text = re.sub(r"^(?:(?:hey|ok|okay),? )?(?:please |can you |could you |would you )*", "", text)
    return re.sub(r"(?: please| for me| now)+$", "", text).strip()


def _app(name: str, safe_apps: frozenset[str]) -> str | None:
    exe = APP_ALIASES.get(name.strip().removeprefix("the ").removesuffix(" app").strip())
    return exe if exe and exe in safe_apps else None


def _spoken_domain(name: str) -> str | None:
    name = name.replace(" dot ", ".")
    if " " in name:  # "c tools thing.exe" is not a spoken web address
        return None
    match = _DOMAIN.match(name)
    return f"https://{match.group(0)}" if match else None


def match_phrase(text: str, safe_apps: frozenset[str]) -> QuickCommand | None:
    """Exact common phrasings only; anything with a second step ("and",
    "then") is left to the full agent."""
    if re.search(r"[\\/:]", text or ""):
        return None  # a path or a URL scheme: the full agent decides what that means
    t = _normalize(text)
    if not t or re.search(r"\b(?:and|then|after that)\b", t):
        return None

    if t in ("volume up", "turn the volume up", "turn volume up", "turn it up", "louder"):
        return QuickCommand("key", "volume_up", REPLIES["volume_up"], "phrase")
    if t in ("volume down", "turn the volume down", "turn volume down", "turn it down", "quieter", "softer"):
        return QuickCommand("key", "volume_down", REPLIES["volume_down"], "phrase")
    if t in ("mute", "unmute", "mute the sound", "mute sound", "toggle mute"):
        return QuickCommand("key", "mute", "Okay.", "phrase")
    if re.fullmatch(r"(?:pause|play|resume)(?: the)?(?: music| song| video| playback)?", t):
        return QuickCommand("key", "play_pause", REPLIES["play_pause"], "phrase")
    if re.fullmatch(r"(?:next|skip)(?: the)?(?: track| song| video)?", t):
        return QuickCommand("key", "next_track", REPLIES["next_track"], "phrase")
    if re.fullmatch(r"(?:previous|last|go back a)(?: track| song| video)", t):
        return QuickCommand("key", "previous_track", REPLIES["previous_track"], "phrase")

    m = re.fullmatch(r"(?:search|google)(?: on)?(?: (youtube|google|wikipedia))?(?: for)? (.{2,120})", t)
    if m:
        engine = m.group(1) or "google"
        query = m.group(2).strip()
        return QuickCommand("open_url", SEARCH_ENGINES[engine].format(q=quote_plus(query)),
                            f"Searching {engine} for {query}.", "phrase")

    m = re.fullmatch(r"(?:open|launch|start|run|go to|visit) (.{2,60})", t)
    if m:
        target = m.group(1).strip()
        exe = _app(target, safe_apps)
        if exe:
            return QuickCommand("open_app", exe, f"Opening {target.removeprefix('the ')}.", "phrase")
        site = target.removeprefix("the ").removesuffix(" website").removesuffix(" site").strip()
        if site in SITES:
            return QuickCommand("open_url", SITES[site], f"Opening {site}.", "phrase")
        url = _spoken_domain(site)
        if url:
            return QuickCommand("open_url", url, f"Opening {site.replace(' dot ', '.')}.", "phrase")
    return None


QUICK_KINDS = {
    "open_app": "Open one desktop application and nothing else.",
    "open_site": "Open one website from the list and nothing else.",
    "volume_up": "Make the sound louder.",
    "volume_down": "Make the sound quieter.",
    "mute": "Mute or unmute the sound.",
    "play_pause": "Pause, play or resume what is playing.",
    "next_track": "Skip to the next song or video.",
    "previous_track": "Go back to the previous song or video.",
    "task": "Anything else: more than one step, typing text, reading or finding information, an app or "
            "site not listed, or not a command at all.",
}


def jev_route(text: str, jev, safe_apps: frozenset[str], min_confidence: float) -> QuickCommand | None:
    """One Jev request: which quick command, if any, and which app/site. Any
    doubt, invalid answer or error -> None (the full agent runs)."""
    apps = {name: f"{name} ({exe})" for name, exe in APP_ALIASES.items() if exe in safe_apps}
    questions = {
        "kind": {"type": "choice", "criteria": QUICK_KINDS, "instructions": (
            "`utterance` is a spoken request to a computer assistant. Is it exactly ONE simple action from this "
            "list? Choose task whenever it needs more than one step or anything not listed.")},
        "site": {"type": "choice", "criteria": {**{s: s for s in SITES}, "none": "none of these"},
                 "instructions": "Assume the request is to open a website: which one?"},
    }
    if apps:
        questions["app"] = {"type": "choice", "criteria": {**apps, "none": "none of these"},
                            "instructions": "Assume the request is to open an application: which one?"}
    try:
        answers, _usage = jev.ask({"utterance": text}, questions)
        kind_answer = validate_choice(answers.get("kind"), QUICK_KINDS)
    except JevError:
        return None
    kind, conf = kind_answer["choice"], float(kind_answer["confidence"])
    if kind == "task" or conf < min_confidence:
        return None
    try:
        if kind == "open_app":
            if not apps:
                return None
            app = validate_choice(answers.get("app"), questions["app"]["criteria"])
            if app["choice"] == "none" or float(app["confidence"]) < min_confidence:
                return None
            return QuickCommand("open_app", APP_ALIASES[app["choice"]], f"Opening {app['choice']}.", "jev")
        if kind == "open_site":
            site = validate_choice(answers.get("site"), questions["site"]["criteria"])
            if site["choice"] == "none" or float(site["confidence"]) < min_confidence:
                return None
            return QuickCommand("open_url", SITES[site["choice"]], f"Opening {site['choice']}.", "jev")
    except JevError:
        return None
    return QuickCommand("key", kind, REPLIES[kind], "jev")


class QuickCommands:
    """Try a quick command; returns the spoken reply, or None to mean "run the
    full agent". Executors are injected so tests/ never touch Windows."""

    def __init__(
        self, safe_apps: frozenset[str], launch_app: Callable[[str], None], open_url: Callable[[str], None],
        press_media_key: Callable[[str], None], jev=None, min_confidence: float = 0.8,
        is_safe_launch: Callable[[str], bool] | None = None, log: Callable[[str], None] = print,
    ):
        self.safe_apps = safe_apps
        self.launch_app = launch_app
        self.open_url = open_url
        self.press_media_key = press_media_key
        self.jev = jev
        self.min_confidence = min_confidence
        # The agent's own risk check for a launch (WindowsToolProvider.
        # get_dynamic_risk() == "R0"); defaults to the SAFE_APPS membership.
        self.is_safe_launch = is_safe_launch or (lambda exe: exe in safe_apps)
        self.log = log

    def route(self, text: str) -> QuickCommand | None:
        command = match_phrase(text, self.safe_apps)
        if command is None and self.jev is not None:
            command = jev_route(text, self.jev, self.safe_apps, self.min_confidence)
        return command

    def try_handle(self, text: str) -> str | None:
        command = self.route(text)
        if command is None:
            return None
        if command.kind == "open_app":
            if not self.is_safe_launch(command.value):
                return None  # not an R0 launch -> the full agent, which asks
            self.launch_app(command.value)
        elif command.kind == "open_url":
            if not command.value.startswith("https://"):
                return None
            self.open_url(command.value)
        else:
            presses = VOLUME_STEPS if command.value in ("volume_up", "volume_down") else 1
            for _ in range(presses):
                self.press_media_key(command.value)
        self.log(f"  [quick] {command.kind} {command.value} (via {command.source})")
        return command.reply


def windows_media_key(name: str) -> None:
    """Press and release one media key, like the keyboard's own."""
    import ctypes

    vk = MEDIA_KEYS[name]
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
