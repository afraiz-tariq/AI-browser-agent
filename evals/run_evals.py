"""
Eval suite runner.

Run this yourself, on your own machine, with a real .env configured
(LLM_PROVIDER=openai or anthropic, a real API key): `python evals/run_evals.py`.
This cannot be run for real inside the sandboxed dev environment this
project is developed in -- there's no live API key there -- which is why
this file refuses to run against LLM_PROVIDER=mock rather than silently
producing meaningless "passes."

What this is for: tests/*.py (189 tests) drive the agent loop with
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
import json
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
    token_usage = record.get("token_usage", {"input_tokens": 0, "output_tokens": 0})
    return EvalResult(task.id, task.description, passed, detail, duration, token_usage)


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

    total_in = sum(r.token_usage.get("input_tokens", 0) for r in ran)
    total_out = sum(r.token_usage.get("output_tokens", 0) for r in ran)
    print("-" * 70)
    print(f"{len(passed)}/{len(ran)} passed, {len(skipped)} skipped.")
    print(f"Total token usage across all ran tasks: {total_in} input, {total_out} output.")
    print("=" * 70)
    return 0 if not failed else 1


def main() -> int:
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

    return _print_report(results)


if __name__ == "__main__":
    sys.exit(main())
