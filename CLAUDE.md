# CLAUDE.md

Orientation only. Don't restate what's already written elsewhere — read it there:

- **`ARCHITECTURE_DECISIONS.md`** is ground truth for *why* things are built this way, and says so explicitly: read it before proposing a framework migration, a rewrite, or "let's research this again." Three external rebuild proposals have already been reviewed and rejected for reasons recorded there (§3, §6) — don't re-litigate them from scratch.
- **`README.md`** is ground truth for *how to run/configure* things (`.env` vars, example tasks, troubleshooting).
- **`CHANGELOG.md`** is the dated *what shipped when*.

Update whichever of those three a change actually touches, in the same commit — that's the existing habit, keep it.

## Non-negotiable invariants

Don't touch these without an explicit ask, and flag it clearly if a task seems to require it:

- Never bypass a login/CAPTCHA/MFA/rate-limit wall. The agent stops and hands control back.
- Risk tiers (`tool_provider.py`): **R1** confirms only if `CONFIRM_R1_ACTIONS` is explicitly turned on (default off). **R2** (disk writes, form submits, `excel_save`; a GET search-box submit is R1, see `BrowserSession.is_search_submit`) confirms by default via `CONFIRM_SENSITIVE_ACTIONS` (default true) — that flag genuinely gates it, don't assume it's ignored. **R3** (sensitive/destructive/unclassified) always confirms, unconditionally, regardless of any flag — this is fail-closed by design, not a toggle to relax.
- A new tool (native arm or MCP) defaults to **R3 (always confirm)** until explicitly risk-classified in code. Never inherit an MCP server's own idea of its tool's risk.
- No secret (API key, cookie, session token) ever reaches a log file. `logger.py` redacts as defense in depth, but don't add a new log call that writes a raw credential and rely on redaction to catch it.
- `tests/` stays fully offline (mock LLM, local fixtures) — no real network/API calls added there. Anything that needs a real LLM belongs in `evals/`.

## Where things live

One arm = one file implementing `ToolProvider` (`tool_provider.py` is the contract, `test_tool_provider.py` is its own test). `agent.py` is the only orchestrator; it dispatches by tool name, never by checking which arm a tool came from.

| Touching... | Arm file | Its tests |
|---|---|---|
| Browser actions, observe/verify, login-wall detection | `browser.py` | `test_browser.py`, `test_agent_loop.py` |
| Excel read/write/save | `excel_tools.py` | `test_excel_tools.py`, `test_excel_arm_integration.py` |
| MCP servers (fetch/Brave/filesystem) | `mcp_tools.py` | `test_mcp_*.py` |
| Windows automation | `windows_tools.py` | `test_windows_tools.py` |
| The loop itself (steps, stuck-loop detection, MAX_STEPS) | `agent.py` | `test_agent_loop.py` |
| LLM provider calls, tool schema building, token usage | `llm.py` | `test_llm_*.py` |
| Optional Jev decider (`DECIDER=hybrid`), Claude fallback | `jev.py` | `test_jev.py` |
| Discord interface, confirm-in-chat | `discord_bot.py` | `test_discord_bot.py` |
| Voice interface, spoken confirm, stop key | `voice.py` | `test_voice.py` |
| Voice window (Yes/No buttons) and tray icon | `voice_ui.py` | `test_voice.py` |
| Voice quick commands (open app/site, volume, media, screenshot) | `quick_commands.py` | `test_quick_commands.py` |
| `.env` parsing/validation | `config.py` | `test_config.py` |
| Risk-tier/confirmation contract itself | `tool_provider.py` | `test_tool_provider.py` |

A change to one arm essentially never requires touching another — use that to scope reading. Don't open `windows_tools.py` to fix a browser bug.

## Dev loop

```
pytest tests/ -v          # offline, no API key needed — run after every change
python evals/run_evals.py # costs real API calls — only when asked, or before claiming a behavioral fix works
```

`pytest -v` output is long (~190 tests). Prefer `pytest tests/test_whatever.py -q` when iterating on one arm, and only run the full `-v` suite once at the end — don't let a full verbose run sit in context repeatedly while iterating.

## Context / subagent habits for this repo

- Reading `ARCHITECTURE_DECISIONS.md`'s relevant section beats re-deriving a design rationale from the code — it's usually already answered there, including *why not* the obvious alternative.
- A full-suite test failure investigation (which of ~190 tests broke, across how many files) is a good subagent task: hand it "run `pytest tests/ -v`, report which tests fail and why" and let it come back with a diagnosis instead of the raw log.
- `evals/run_evals.py` output and any exploratory `python agent.py "<task>"` run against a real site produces a lot of step-by-step trace noise — redirect to a file (`> /tmp/run.log`) and read back only the final result/error unless debugging that specific run.
- Bug hunts that need reproducing something via real end-to-end runs (the pattern behind most of the numbered bugs in `ARCHITECTURE_DECISIONS.md` §1) found real issues that unit tests alone didn't — don't assume a green `pytest` run means an arm actually works against a live target.
