# Eval suite

`tests/*.py` (259 tests) drive the agent loop with `MockProvider` --
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
python evals/run_evals.py --save evals/results/baseline.json   # also keep a JSON copy to compare later runs against
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

Each task also prints its step count and median per-step decide / observe /
act time, and the report ends with the same medians across every task. These
come from the `timings` block every `output/*.json` record now carries.
Time spent waiting for a human `[y/n]` answer is excluded from act. This is
the speed baseline `docs/JEV_VOICE_PLAN.md` Phase 0 asks for: run it once
with `--save`, and later runs (another `LLM_MODEL`, or a future `DECIDER`)
can be compared against that file.

`--save` writes task ids, pass/fail, details, timings and token counts only,
no API keys and no page contents.

A `SKIP` is not a failure -- it means that task's `requires(config)` was
false for your current `.env` (e.g. you haven't turned on
`ENABLE_MCP_FETCH`). Turn on the relevant flag to include it.

## Baseline: claude-sonnet-5, 2026-09-24

Measured on the user's Windows PC after the one-call page snapshot and prompt
caching landed. Raw file: `results/2026-09-24-claude-sonnet-5.json`.

| | Value |
|---|---|
| Tasks passed | 9 / 9 (3 MCP tasks skipped: not enabled) |
| Steps (decisions) | 36 (35) |
| Median time per step: Claude decision | **2,275 ms** |
| Median time per step: reading the page | 53 ms |
| Median time per step: doing the action | 21 ms (a first `goto` is ~1.5-2.7 s: Chrome launching) |
| Share of total task time spent waiting on Claude | 81 s of 104 s (**78%**) |
| Prompt tokens per decision | ~6,600, of which 84% read from the prompt cache |
| Output tokens per decision | ~100 |
| Cost for the whole run | ~$0.15 (would be ~$0.50 without caching), ~$0.004 per decision |

Costs are computed from the token counts at Sonnet 5 list prices ($2 / $10
per million, cache reads 0.1x, 5-minute cache writes 1.25x); the API doesn't
return dollar amounts.

What it says: after the snapshot fix, the model call is essentially the whole
per-step wait. Cost is already small, so the case for a faster decider (Jev,
see `docs/JEV_VOICE_PLAN.md`) is speed, not money: e.g. the 10-step Calculator
task spent ~20 of its 22.5 s waiting on decisions.

## Page-reading speed (free, offline)

```
python evals/bench_observe.py
```

Times `BrowserSession.observe()` on generated local pages of 50/200/500
interactive elements. No LLM, no API key, no internet, so it costs nothing
and is safe to run after any change to `observe()`. It needs the same
Chrome/`.env` browser settings as a normal run (it forces headless).

## Testing the harness itself

`tests/test_eval_harness.py` covers `run_single_eval()`'s own mechanics
(scoring, skip handling, teardown-on-error, reading `token_usage` back out
of the saved output record) using `MockProvider`, the same "verify the
mechanism, not the model" split as the rest of `tests/`. It runs as part
of the normal `pytest tests/` suite and needs no API key -- it's a
guarantee that the *runner* is correct, not that the *tasks* pass for real.
