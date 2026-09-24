"""
Tests for quick_commands.py -- the voice shortcut for one-step commands.
Offline: Windows actions are recorded by fakes, Jev is simulated with the
same scripted transport as tests/test_jev.py.
"""
import httpx
import pytest

from jev import JevClient
from quick_commands import QuickCommands, match_phrase
from tests.test_jev import ScriptedJev
from voice import VoiceAssistant

SAFE = frozenset({"notepad.exe", "calc.exe", "mspaint.exe", "snippingtool.exe", "explorer.exe"})


def _quick(jev=None, safe=SAFE, is_safe_launch=None):
    done = []
    quick = QuickCommands(
        safe, launch_app=lambda exe: done.append(("launch", exe)), open_url=lambda url: done.append(("url", url)),
        press_media_key=lambda key: done.append(("key", key)), jev=jev, is_safe_launch=is_safe_launch,
        log=lambda m: None,
    )
    return quick, done


@pytest.mark.parametrize("said, expected", [
    ("Open notepad.", ("launch", "notepad.exe")),
    ("please open the calculator", ("launch", "calc.exe")),
    ("Launch Paint", ("launch", "mspaint.exe")),
    ("go to YouTube", ("url", "https://www.youtube.com")),
    ("open example dot com", ("url", "https://example.com")),
    ("search youtube for lofi beats", ("url", "https://www.youtube.com/results?search_query=lofi+beats")),
    ("Google best ramen near me", ("url", "https://www.google.com/search?q=best+ramen+near+me")),
    ("pause the music", ("key", "play_pause")),
    ("next track", ("key", "next_track")),
    ("mute", ("key", "mute")),
])
def test_common_phrases_run_instantly_without_jev_or_claude(said, expected):
    quick, done = _quick()
    assert quick.try_handle(said)
    assert done[0] == expected


def test_volume_moves_a_noticeable_step():
    quick, done = _quick()
    assert quick.try_handle("volume up") == "Volume up."
    assert done == [("key", "volume_up")] * 5


@pytest.mark.parametrize("said", [
    "open notepad and type hello world",      # more than one step
    "open chrome",                             # not on SAFE_APPS -> full agent, which asks
    "open C:\\\\tools\\\\thing.exe",
    "look up the weather in Paris",            # wants an answer read back, not a results page
    "stop",                                    # F10 stops tasks; "stop" must not toggle music
    "what's on my calendar today",
    "",
])
def test_anything_else_goes_to_the_full_agent(said):
    quick, done = _quick()
    assert quick.try_handle(said) is None
    assert done == []


def test_an_app_is_launched_only_if_the_agents_own_risk_check_says_r0():
    quick, done = _quick(is_safe_launch=lambda exe: False)  # e.g. SAFE_APPS emptied in .env
    assert quick.try_handle("open notepad") is None
    assert done == []


def test_apps_removed_from_safe_apps_are_not_quick():
    quick, done = _quick(safe=frozenset({"calc.exe"}))
    assert quick.try_handle("open notepad") is None
    assert quick.try_handle("open calculator") == "Opening calculator."


def test_only_plain_domains_become_urls():
    assert match_phrase("go to javascript:alert(1)", SAFE) is None
    assert match_phrase("open my documents folder", SAFE) is None


# --- Jev for other phrasings --------------------------------------------------

def _jev(*script):
    return JevClient("test-key", transport=httpx.MockTransport(ScriptedJev(*script)))


def test_jev_catches_a_paraphrase_the_phrase_list_misses():
    quick, done = _quick(jev=_jev({"kind": "open_app", "app": "calculator"}))
    assert quick.try_handle("fire up the calculator thing") == "Opening calculator."
    assert done == [("launch", "calc.exe")]


def test_jev_media_paraphrase():
    quick, done = _quick(jev=_jev({"kind": "volume_down"}))
    assert quick.try_handle("it's a bit loud in here") == "Volume down."


@pytest.mark.parametrize("script", [
    {"kind": "task"},                               # Jev says it's a real task
    {"kind": "open_app", "app": "none"},            # no listed app fits
    {"kind": "volume_up", "_conf": 0.6},            # not sure enough (floor 0.8)
    {"kind": "open_site", "site": "nonexistent"},   # an answer outside the offered ids
])
def test_jev_doubt_means_the_full_agent_runs(script):
    quick, done = _quick(jev=_jev(script))
    assert quick.try_handle("do the thing I mentioned earlier") is None
    assert done == []


def test_jev_down_means_the_full_agent_runs():
    unreachable = JevClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    quick, done = _quick(jev=unreachable)
    assert quick.try_handle("make it quieter please friend") is None


# --- voice hand-off -------------------------------------------------------------

def test_voice_uses_the_quick_path_and_skips_the_agent():
    quick, done = _quick()
    said, runs = [], []
    assistant = VoiceAssistant(None, lambda a: "Open notepad.", said.append, lambda s: None,
                               lambda *a, **k: runs.append(a), log=lambda m: None, quick=quick)
    outcome = assistant.handle_audio("audio")
    assert outcome == {"success": True, "result": "Opening notepad.", "quick": True}
    assert said == ["Opening notepad."] and runs == [] and done == [("launch", "notepad.exe")]


def test_voice_falls_back_to_the_agent_if_the_shortcut_errors():
    def broken(exe):
        raise OSError("launch failed")

    quick = QuickCommands(SAFE, launch_app=broken, open_url=lambda u: None, press_media_key=lambda k: None,
                          log=lambda m: None)
    runs = []
    assistant = VoiceAssistant(None, lambda a: "open notepad", lambda t: None, lambda s: None,
                               lambda text, config, **k: runs.append(text) or {"success": True, "result": "ok"},
                               log=lambda m: None, quick=quick)
    assistant.handle_audio("audio")
    assert runs == ["open notepad"]
