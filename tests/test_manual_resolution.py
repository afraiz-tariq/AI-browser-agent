"""
Tests for agent.py's offer_manual_resolution() -- the "a human can solve
this login wall live in the visible Chrome window" mechanism. Had zero
test coverage: nothing exercised its headless/dry-run short-circuit, its
attempt-limit cutoff, or its accept/decline parsing, despite this being
the one place in the loop that calls input() directly and so is the one
place a bug here would actually hang the process waiting on a terminal
that (in headless/bot use) is never attached.
"""
from types import SimpleNamespace

import pytest

from agent import MAX_MANUAL_RESOLUTION_OFFERS, offer_manual_resolution
from logger import TaskLogger


def _logger(tmp_path) -> TaskLogger:
    return TaskLogger(tmp_path, "a task")


def test_headless_never_prompts_and_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("input() must not be called when headless"))
    config = SimpleNamespace(headless=True)

    result = offer_manual_resolution("https://example.com", config, dry_run=False, logger=_logger(tmp_path), attempt=1)

    assert result is False


def test_dry_run_never_prompts_and_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("input() must not be called during dry_run"))
    config = SimpleNamespace(headless=False)

    result = offer_manual_resolution("https://example.com", config, dry_run=True, logger=_logger(tmp_path), attempt=1)

    assert result is False


def test_exceeding_the_attempt_limit_gives_up_without_prompting(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("input() must not be called past the limit"))
    config = SimpleNamespace(headless=False)

    result = offer_manual_resolution(
        "https://example.com", config, dry_run=False, logger=_logger(tmp_path),
        attempt=MAX_MANUAL_RESOLUTION_OFFERS + 1,
    )

    assert result is False


def test_at_the_attempt_limit_still_prompts(tmp_path, monkeypatch):
    # attempt == the limit is the LAST allowed try, not already over it.
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    config = SimpleNamespace(headless=False)

    result = offer_manual_resolution(
        "https://example.com", config, dry_run=False, logger=_logger(tmp_path), attempt=MAX_MANUAL_RESOLUTION_OFFERS,
    )

    assert result is True


@pytest.mark.parametrize("answer", ["", "done", "ok", "continue", "y"])
def test_a_non_giveup_answer_means_continue(tmp_path, monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    config = SimpleNamespace(headless=False)

    result = offer_manual_resolution("https://example.com", config, dry_run=False, logger=_logger(tmp_path), attempt=1)

    assert result is True


@pytest.mark.parametrize("answer", ["stop", "STOP", "quit", "exit", "n", "  stop  "])
def test_a_giveup_answer_means_stop(tmp_path, monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    config = SimpleNamespace(headless=False)

    result = offer_manual_resolution("https://example.com", config, dry_run=False, logger=_logger(tmp_path), attempt=1)

    assert result is False
