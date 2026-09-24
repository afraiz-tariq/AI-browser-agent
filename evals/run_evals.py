"""
Eval suite runner.

Run this yourself, on your own machine, with a real .env configured
(LLM_PROVIDER=openai or anthropic, a real API key): `python evals/run_evals.py`.
This cannot be run for real inside the sandboxed dev environment this
project is developed in -- there's no live API key there -- which is why
this file refuses to run against LLM_PROVIDER=mock rather than silently
producing meaningless "passes."

What this is for: tests/*.py (259 tests) drive the agent loop with
MockProvider -- scripted replies -- to prove the *mechanism* is correct
(dispatch, risk gating, verify, pagination, ...). None of them ever ask a
real model to reason its way through a task. This suite does exactly
that: real tasks, a real configured LLM, scored against what the agent
actually did (a saved file's contents, not just a claimed summary).

Each task runs as its own real run_task() call -- real Chrome, real
openpyxl files, real MCP servers where enabled -- so this costs real LLM
API calls (and a Brave Search API call, if that arm is enabled). It is
not free to run and not meant to run in CI.
"""
from __future__ import annotations

import dataclasses
import functools
import http.server
import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import run_task  # noqa: E402
from config import load_config  # noqa: E402
from evals.tasks import TASKS, EvalContext, EvalTask  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


class _FixturesServer:
    """Same local static-file-server pattern as tests/conftest.py's
    `fixtures_server` fixture, standalone here since this runner isn't
    pytest. Serves tests/fixtures/ so browser tasks have deterministic,
    offline pages to act on instead of depending on live sites."""

    def __init__(self) -> None:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES_DIR))
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)


@dataclasses.dataclass
class EvalResult:
    task_id: str
    description: str
    passed: bool | None  # None means skipped
    detail: str
    duration_s: float
    token_usage: dict[str, int]
    # agent.py's summarize_timings() record (per-step observe/decide/act ms,
    # plus medians) -- the Phase 0 baseline in docs/JEV_VOICE_PLAN.md.
    timings: dict = dataclasses.field(default_factory=dict)


def run_single_eval(
    task: EvalTask, config, tmp_path: Path, fixtures_server: str | None, llm_client=None,
) -> EvalResult:
    """Runs one EvalTask end-to-end against `config` and scores it. Split
    out from main() so the harness's own mechanics (this function) can be
    exercised in tests/ by injecting a MockProvider-backed `llm_client`,
    without needing a real API key -- see tests/test_eval_harness.py.
    `llm_client=None` (the real-run path) makes run_task() build one from
    `config` itself, exactly like a normal `python agent.py` run."""
    if not task.requires(config):
        return EvalResult(task.id, task.description, None, "Skipped: requires(config) was False.", 0.0, {})

    ctx = EvalContext(config=config, tmp_path=tmp_path, fixtures_server=fixtures_server)
    task_text = task.build(ctx)

    start = time.monotonic()
    outcome = run_task(
        task_text, config, dry_run=task.dry_run, llm_client=llm_client,
        confirm_callback=task.confirm_callback or (lambda prompt: True),
    )
    duration = time.monotonic() - start

    try:
        passed, detail = task.check(outcome, ctx)
    finally:
        if task.teardown is not None:
            task.teardown(ctx)

    record = json.loads(Path(outcome["output_path"]).read_text(encoding="utf-8"))
    token_usage = record.get("token_usage", {"input_tokens": 0, "output_tokens": 0})  # older records lack cache fields
    return EvalResult(task.id, task.description, passed, detail, duration, token_usage, record.get("timings", {}))


def _all_steps(results: list[EvalResult], key: str) -> list[float]:
    return [s[key] for r in results for s in r.timings.get("steps", []) if key in s]


def speed_summary(results: list[EvalResult]) -> dict:
    """Across every ran task: median per-step observe/decide/act ms, median
    steps and seconds per task, and total tokens -- the numbers a later
    DECIDER=jev|hybrid run gets compared against."""
    ran = [r for r in results if r.passed is not None]

    def med(values):
        return round(statistics.median(values), 1) if values else None

    return {
        "tasks_ran": len(ran),
        "tasks_passed": sum(1 for r in ran if r.passed),
        "median_observe_ms": med(_all_steps(ran, "observe_ms")),
        "median_decide_ms": med(_all_steps(ran, "decide_ms")),
        "median_act_ms": med(_all_steps(ran, "act_ms")),
        "median_steps_per_task": med([len(r.timings.get("steps", [])) for r in ran]),
        "median_seconds_per_task": med([r.duration_s for r in ran]),
        "input_tokens": sum(r.token_usage.get("input_tokens", 0) for r in ran),
        "output_tokens": sum(r.token_usage.get("output_tokens", 0) for r in ran),
        "cache_read_input_tokens": sum(r.token_usage.get("cache_read_input_tokens", 0) for r in ran),
        "cache_creation_input_tokens": sum(r.token_usage.get("cache_creation_input_tokens", 0) for r in ran),
        # DECIDER=hybrid only (jev.py): who decided each step, and how fast.
        "jev_decisions": sum(r.token_usage.get("jev_decisions", 0) for r in ran),
        "claude_escalations": sum(r.token_usage.get("claude_escalations", 0) for r in ran),
        "jev_input_tokens": sum(r.token_usage.get("jev_input_tokens", 0) for r in ran),
        "median_decide_ms_jev_steps": med([s["decide_ms"] for r in ran for s in r.timings.get("steps", [])
                                           if s.get("decider") == "jev" and "decide_ms" in s]),
        "median_decide_ms_claude_steps": med([s["decide_ms"] for r in ran for s in r.timings.get("steps", [])
                                              if s.get("decider") == "claude" and "decide_ms" in s]),
    }


def _print_report(results: list[EvalResult]) -> int:
    ran = [r for r in results if r.passed is not None]
    passed = [r for r in ran if r.passed]
    failed = [r for r in ran if not r.passed]
    skipped = [r for r in results if r.passed is None]

    print("\n" + "=" * 70)
    print("EVAL REPORT")
    print("=" * 70)
    for r in results:
        if r.passed is None:
            status = "SKIP"
        elif r.passed:
            status = "PASS"
        else:
            status = "FAIL"
        print(f"[{status}] {r.task_id} ({r.duration_s:.1f}s) -- {r.description}")
        print(f"       {r.detail}")
        if r.timings.get("steps"):
            print(f"       {len(r.timings['steps'])} steps, median decide {r.timings.get('median_decide_ms')} ms, "
                  f"observe {r.timings.get('median_observe_ms')} ms, act {r.timings.get('median_act_ms')} ms")

    total_in = sum(r.token_usage.get("input_tokens", 0) for r in ran)
    total_out = sum(r.token_usage.get("output_tokens", 0) for r in ran)
    print("-" * 70)
    print(f"{len(passed)}/{len(ran)} passed, {len(skipped)} skipped.")
    print(f"Total token usage across all ran tasks: {total_in} input, {total_out} output.")
    speed = speed_summary(results)
    print(f"Prompt cache: {speed['cache_read_input_tokens']} tokens read from cache (~0.1x price), "
          f"{speed['cache_creation_input_tokens']} written (~1.25x).")
    if speed["jev_decisions"] or speed["claude_escalations"]:
        print(f"Jev decided {speed['jev_decisions']} steps (median {speed['median_decide_ms_jev_steps']} ms); "
              f"Claude decided {speed['claude_escalations']} (median {speed['median_decide_ms_claude_steps']} ms, "
              f"incl. the Jev call first when Jev was asked).")
    print(f"Median per step: decide {speed['median_decide_ms']} ms, observe {speed['median_observe_ms']} ms, "
          f"act {speed['median_act_ms']} ms. Median per task: {speed['median_steps_per_task']} steps, "
          f"{speed['median_seconds_per_task']} s.")
    print("=" * 70)
    return 0 if not failed else 1


def save_results(results: list[EvalResult], config, path: Path) -> None:
    """Machine-readable copy of the report, so a baseline and a later run
    (e.g. a different LLM_MODEL or DECIDER) can be compared side by side.
    Holds only task ids, pass/fail, timings and token counts -- no API keys,
    no page contents."""
    payload = {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "llm_provider": config.llm_provider,
        "llm_model": config.llm_model,
        "decider": getattr(config, "decider", "claude"),
        "summary": speed_summary(results),
        "tasks": [dataclasses.asdict(r) for r in results],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved results to {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the eval suite against a real, configured LLM.")
    parser.add_argument("--save", type=Path, help="also write the results as JSON to this path")
    args = parser.parse_args(argv)
    config = load_config()

    if config.llm_provider == "mock":
        print(
            "Refusing to run: LLM_PROVIDER=mock. Evals against a scripted mock model are "
            "meaningless -- set LLM_PROVIDER=openai or anthropic (with a real API key) in .env "
            "and run this again. See evals/README.md."
        )
        return 1

    problems = config.validate()
    if problems:
        print("Refusing to run: config problems must be fixed first:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    # Overrides for an unattended eval run: headless (no display needed,
    # and nothing here needs a human to solve a wall) and a scratch Chrome
    # profile (don't touch the user's real persistent profile/history).
    # Everything else -- provider, model, API keys, which MCP arms are
    # on -- stays exactly as the user configured it, since that's what's
    # actually being evaluated.
    eval_config = dataclasses.replace(config, headless=True, use_persistent_profile=False)

    with tempfile.TemporaryDirectory() as tmp, _FixturesServer() as fixtures_server:
        tmp_path = Path(tmp)
        results = []
        for task in TASKS:
            print(f"Running {task.id}...")
            results.append(run_single_eval(task, eval_config, tmp_path, fixtures_server))

    exit_code = _print_report(results)
    if args.save:
        save_results(results, eval_config, args.save)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
