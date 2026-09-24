"""
Tests for jev.py -- the optional TypeSafe Jev decider (DECIDER=hybrid).
Fully offline: TypeSafe is simulated with httpx.MockTransport answering from
a script in the same answer shape the real API uses (per the reference
projects), and Claude is the usual MockProvider.
"""
import json

import httpx
import pytest

from agent import run_task
from browser import ElementInfo, Observation
from config import Config
from jev import JevClient, JevDecider, JevError, noul_probability, text_candidates, validate_choice
from llm import LLMClient, MockProvider

KEY = "ts-test-KEY-should-never-appear"


def _answer(choice, criteria, conf=0.9):
    keys = list(criteria)
    rest = round((1 - conf) / max(len(keys) - 1, 1), 4)
    return {"choice": choice, "confidence": conf, "probabilities": {k: (conf if k == choice else rest) for k in keys}}


class ScriptedJev:
    """An httpx transport playing TypeSafe. Each request consumes one script
    entry: {question_name: choice id, or a probability for a noul}. Choice
    questions not named get their first option; conf applies to all."""

    def __init__(self, *script, conf=0.9, status=200):
        self.script = list(script)
        self.conf = conf
        self.status = status
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"body": body, "auth": request.headers.get("authorization")})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        picks = self.script.pop(0) if self.script else {}
        answers = {}
        for name, q in body["questions"].items():
            if q["type"] == "noul":
                answers[name] = {"noul": float(picks.get(name, 0.1)), "confidence": 0.8}
            else:
                pick = picks.get(name, next(iter(q["criteria"])))
                answers[name] = _answer(pick, q["criteria"], picks.get("_conf", self.conf))
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 1000, "output_tokens": 0}})


def _client(scripted: ScriptedJev) -> JevClient:
    return JevClient(KEY, transport=httpx.MockTransport(scripted))


def _obs(elements, url="https://example.com", looks_like_login=False, truncated=False):
    return Observation(
        url=url, title="Page", elements=elements, visible_text="Some page text", looks_like_login=looks_like_login,
        state_fingerprint="", text_truncated=truncated, total_text_length=14,
    )


ELEMENTS = [
    ElementInfo(index=0, tag="input", role="input", text="Search", input_type="text", state=""),
    ElementInfo(index=1, tag="button", role="button", text="Go", input_type=""),
    ElementInfo(index=2, tag="input", role="input", text="Password", input_type="password", state="[hidden]"),
]


def _decider(scripted, claude_replies=()):
    mock = MockProvider(list(claude_replies))
    return JevDecider(LLMClient(mock), _client(scripted)), mock


# --- validation -------------------------------------------------------------

def test_validate_choice_accepts_an_offered_choice():
    ans = _answer("a", {"a": 1, "b": 1})
    assert validate_choice(ans, ["a", "b"]) is ans


@pytest.mark.parametrize("answer", [
    None,
    {"choice": "zzz", "confidence": 0.9, "probabilities": {"a": 0.9}},          # not offered
    {"choice": "a", "confidence": 1.5, "probabilities": {"a": 0.9}},            # not a probability
    {"choice": "a", "confidence": float("nan"), "probabilities": {"a": 0.9}},   # NaN
    {"choice": "a", "confidence": 0.9, "probabilities": {"a": 0.9, "x": 0.1}},  # unknown id in probabilities
    {"choice": "a", "probabilities": {"a": 0.9}},                              # missing confidence
])
def test_validate_choice_rejects_anything_else(answer):
    with pytest.raises(JevError):
        validate_choice(answer, ["a", "b"])


def test_noul_probability_reads_both_answer_shapes_and_defaults_to_no():
    assert noul_probability({"noul": 0.8}) == 0.8
    assert noul_probability({"probabilities": {"true": 0.3, "false": 0.7}}) == 0.3
    assert noul_probability({"noul": 7}) == 0.0
    assert noul_probability(None) == 0.0


def test_text_candidates_come_only_from_the_task_words():
    assert text_candidates("Search for 'openai' and tell me the city.") == ["openai"]
    assert "lofi beats" in text_candidates("go to youtube and search for lofi beats, then play one")
    assert text_candidates("Click the second result.") == []


# --- client -----------------------------------------------------------------

def test_client_sends_the_key_only_as_a_bearer_header_and_the_expected_body():
    scripted = ScriptedJev({})
    _client(scripted).ask({"s": 1}, {"q": {"type": "choice", "criteria": {"a": "A"}, "instructions": "x"}})
    sent = scripted.requests[0]
    assert sent["auth"] == f"Bearer {KEY}"
    assert set(sent["body"]) == {"model", "state", "questions"}
    assert KEY not in json.dumps(sent["body"])


def test_client_errors_never_contain_the_key():
    with pytest.raises(JevError) as e:
        _client(ScriptedJev(status=401)).ask({}, {})
    assert "401" in str(e.value) and KEY not in str(e.value)


def test_client_retries_a_server_error_once(monkeypatch):
    calls = []

    def flaky(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"answers": {"x": 1}, "usage": {}})

    answers, _ = JevClient(KEY, transport=httpx.MockTransport(flaky)).ask({}, {})
    assert answers == {"x": 1} and len(calls) == 2


def test_client_requires_a_key():
    with pytest.raises(JevError):
        JevClient("")


# --- decider ----------------------------------------------------------------

CLAUDE_FINISH = {"action": "finish", "thought": "Claude here.", "args": {"summary": "Answer from Claude."}}


def test_click_with_confidence_is_decided_by_jev():
    decider, mock = _decider(ScriptedJev({"operation": "CLICK", "click_target": "1"}))
    decision = decider.decide_next_action("Press Go.", [], _obs(ELEMENTS))
    assert decision["action"] == "click" and decision["args"] == {"index": 1}
    assert decision["decider"] == "jev"
    assert mock.calls == []  # Claude never asked


def test_low_confidence_escalates_to_claude():
    decider, mock = _decider(ScriptedJev({"operation": "CLICK", "click_target": "1", "_conf": 0.3}), [CLAUDE_FINISH])
    decision = decider.decide_next_action("Press Go.", [], _obs(ELEMENTS))
    assert decision["decider"] == "claude" and decision["action"] == "finish"
    assert "unsure" in decision["escalation_reason"]


@pytest.mark.parametrize("operation", ["DONE", "OTHER"])
def test_done_and_other_go_to_claude(operation):
    # DONE: Claude checks the page and writes the finish summary (keeps the
    # "summary must have real content" guarantee). OTHER: a URL, Excel, a
    # login page... -- things Jev can't express.
    decider, mock = _decider(ScriptedJev({"operation": operation}), [CLAUDE_FINISH])
    decision = decider.decide_next_action("Find the answer.", [], _obs(ELEMENTS))
    assert decision["decider"] == "claude" and len(mock.calls) == 1


def test_an_answer_outside_the_offered_ids_never_executes():
    decider, mock = _decider(ScriptedJev({"operation": "CLICK", "click_target": "999"}), [CLAUDE_FINISH])
    decision = decider.decide_next_action("Press Go.", [], _obs(ELEMENTS))
    assert decision["decider"] == "claude"
    assert "Invalid answer" in decision["escalation_reason"]


def test_type_text_uses_a_span_of_the_task_and_never_targets_a_password_field():
    scripted = ScriptedJev({"operation": "TYPE_TEXT", "type_target": "0", "type_value": "1", "type_submit": 0.9})
    decider, _ = _decider(scripted)
    decision = decider.decide_next_action("Search for 'openai' please.", [], _obs(ELEMENTS))
    assert decision["action"] == "type"
    assert decision["args"] == {"index": 0, "text": "openai", "submit": True}
    questions = scripted.requests[0]["body"]["questions"]
    assert "2" not in questions["type_target"]["criteria"]  # the password field is not offered
    assert "0" in questions["type_target"]["criteria"]


def test_type_text_is_not_offered_when_the_task_has_nothing_to_type():
    scripted = ScriptedJev({"operation": "CLICK", "click_target": "1"})
    decider, _ = _decider(scripted)
    decider.decide_next_action("Press the Go button.", [], _obs(ELEMENTS))
    questions = scripted.requests[0]["body"]["questions"]
    assert "TYPE_TEXT" not in questions["operation"]["criteria"]
    assert "type_target" not in questions


def test_secret_values_never_reach_jev():
    scripted = ScriptedJev({"operation": "CLICK", "click_target": "1"})
    decider, _ = _decider(scripted)
    decider.decide_next_action("Press Go.", [], _obs(ELEMENTS))
    assert "[hidden]" in json.dumps(scripted.requests[0]["body"])  # masked by browser.py, passed through as-is


def test_no_page_and_login_walls_go_straight_to_claude_without_asking_jev():
    scripted = ScriptedJev()
    decider, mock = _decider(scripted, [CLAUDE_FINISH, CLAUDE_FINISH])
    assert decider.decide_next_action("Open a site.", [], None)["decider"] == "claude"
    assert decider.decide_next_action("Log in.", [], _obs(ELEMENTS, looks_like_login=True))["decider"] == "claude"
    assert scripted.requests == []


def test_jev_being_down_falls_back_to_claude():
    decider, mock = _decider(ScriptedJev(status=503), [CLAUDE_FINISH])
    decision = decider.decide_next_action("Press Go.", [], _obs(ELEMENTS))
    assert decision["decider"] == "claude"


def test_usage_reports_jev_and_claude_side_by_side():
    decider, _ = _decider(ScriptedJev({"operation": "CLICK", "click_target": "1"}, {"operation": "DONE"}),
                          [CLAUDE_FINISH])
    decider.decide_next_action("Press Go.", [], _obs(ELEMENTS))
    decider.decide_next_action("Press Go.", [], _obs(ELEMENTS))
    usage = decider.get_usage()
    assert usage["jev_requests"] == 2 and usage["jev_decisions"] == 1 and usage["claude_escalations"] == 1
    assert usage["jev_input_tokens"] == 2000


# --- config -----------------------------------------------------------------

def test_hybrid_without_a_key_is_a_config_problem():
    problems = Config(llm_provider="mock", decider="hybrid", typesafe_api_key="").validate()
    assert any("TYPESAFE_API_KEY" in p for p in problems)
    assert Config(llm_provider="mock", decider="claude").validate() == []


# --- through the real loop, real browser, local fixtures ----------------------

def _reply(thought, action, args):
    return json.dumps({"thought": thought, "action": action, "args": args})


def test_hybrid_task_end_to_end_on_the_fixture_search_engine(test_config, fixtures_server):
    # Claude opens the page (no page yet), Jev types + submits the search,
    # then the results page has no elements to pick from, so Claude reads it
    # and writes the summary without Jev being asked.
    scripted = ScriptedJev(
        {"operation": "TYPE_TEXT", "type_target": "0", "type_value": "1", "type_submit": 0.9},
    )
    mock = MockProvider([
        _reply("Opening.", "goto", {"url": f"{fixtures_server}/index.html"}),
        _reply("Done.", "finish", {"summary": "The results page lists OpenAI, based in San Francisco."}),
    ])
    decider = JevDecider(LLMClient(mock), _client(scripted))

    outcome = run_task("Search for 'openai' and report the city.", test_config, dry_run=True, llm_client=decider)

    assert outcome["success"] is True
    record = json.loads(open(outcome["output_path"], encoding="utf-8").read())
    assert [s.get("decider") for s in record["timings"]["steps"]] == ["claude", "jev", "claude"]
    assert record["token_usage"]["jev_decisions"] == 1
    assert len(scripted.requests) == 1
    assert "index.html" in scripted.requests[0]["body"]["state"]["page"]["url"]


def test_a_jev_chosen_sensitive_click_still_asks_for_confirmation(test_config, fixtures_server):
    # Jev never classifies risk: its click goes through the same dispatch
    # and risk tiers, so a declined confirmation still stops the task.
    scripted = ScriptedJev({"operation": "CLICK", "click_target": "0"})
    mock = MockProvider([_reply("Opening.", "goto", {"url": f"{fixtures_server}/sensitive_button.html"})])
    decider = JevDecider(LLMClient(mock), _client(scripted))
    asked = []

    outcome = run_task(
        "Delete the account.", test_config, dry_run=False, llm_client=decider,
        confirm_callback=lambda prompt: asked.append(prompt) or False,
    )

    assert outcome["success"] is False
    assert asked and "declined" in outcome["result"].lower()
