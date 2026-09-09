# Eval suite

`tests/*.py` (217 tests) drive the agent loop with `MockProvider` --
scripted replies -- to prove the *mechanism* is correct: dispatch, risk
gating, the VERIFY step, text pagination, stuck-loop detection, and so on.
None of those tests ever ask a real model to reason its way through
anything, so they can't tell you whether the agent actually completes real
tasks well.

This directory does that instead: a small set of representative tasks run
against a **real, configured LLM**, scored by inspecting what the agent
actually did (a saved file's contents, a correctly-declined confirmation,
the right fact extracted from a page) rather than trusting its own summary.

## Running it

You need a real `.env` (copy `.env.example` if you haven't already) with
`LLM_PROVIDER=openai` or `LLM_PROVIDER=anthropic` and a real API key. This
cannot be run inside a sandboxed dev environment with no live API key --
`run_evals.py` refuses to run at all against `LLM_PROVIDER=mock`, since a
"pass" against a scripted mock would be meaningless.

```
python evals/run_evals.py
```

This costs real LLM API calls -- one full `run_task()` run per task below.
If you've turned on `ENABLE_MCP_BRAVE_SEARCH`, it also costs one real Brave
Search API call. It is not free, and not meant to run in CI.

The suite forces `HEADLESS=true` and a fresh, non-persistent Chrome profile
for the run regardless of your `.env` settings (no display needed, and
nothing here needs a human to solve a login wall) -- everything else
(provider, model, API keys, which MCP arms are on) stays exactly as you've
configured it, since that's what's actually being evaluated.

## What it covers

- **Browser, local fixtures** (`tests/fixtures/`, deterministic and free):
  search-and-extract, login-wall detection (the agent must stop, not try
  to bypass it), a sensitive action correctly declined via `[y/n]`
  confirmation, and reading content near the end of a page longer than one
  observation window (`scroll` pagination).
- **Browser, live web** (one task): reading a heading off a real, stable
  public page (`example.com`) -- the one task that also exercises genuine
  open-web capability rather than only fixed local pages.
- **Excel**: a create/write/save roundtrip verified by re-opening the saved
  file, and reading back a value from a pre-populated workbook.
- **Mixed browser + Excel**: extracting a fact via the browser arm, then
  persisting it via the Excel arm, in one task.
- **MCP arms** (each skipped cleanly, not failed, if its `ENABLE_MCP_*`
  flag is off): fetch, local filesystem read, Brave Search.
- **Windows desktop automation** (skipped cleanly if `ENABLE_WINDOWS_AUTOMATION`
  is off, and only runnable on Windows in the first place): launch
  Calculator, compute 7 + 3 via its real UI controls, and report the result.

See `tasks.py` for the exact task text and `check()` functions.

## Reading the report

Each task prints `PASS`, `FAIL`, or `SKIP` with a one-line reason, and the
report ends with a pass/fail count and the total token usage (input +
output) across every task that actually ran -- read from the same
structured `output/*.json` records every real run writes (see
`ARCHITECTURE_DECISIONS.md`).

A `SKIP` is not a failure -- it means that task's `requires(config)` was
false for your current `.env` (e.g. you haven't turned on
`ENABLE_MCP_FETCH`). Turn on the relevant flag to include it.

## Testing the harness itself

`tests/test_eval_harness.py` covers `run_single_eval()`'s own mechanics
(scoring, skip handling, teardown-on-error, reading `token_usage` back out
of the saved output record) using `MockProvider`, the same "verify the
mechanism, not the model" split as the rest of `tests/`. It runs as part
of the normal `pytest tests/` suite and needs no API key -- it's a
guarantee that the *runner* is correct, not that the *tasks* pass for real.
