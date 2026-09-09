# Changelog

A dated, human-skimmable record of what shipped and when -- `git log` has
the same information but isn't skimmable. See `ARCHITECTURE_DECISIONS.md`
for *why* things are built the way they are; this file is just *what* and
*when*.

**Keeping this updated:** add a dated entry (or a new bullet under today's
date if one already exists) as part of the same commit that ships a
feature, fix, or notable change -- same habit as updating README.md/
ARCHITECTURE_DECISIONS.md when a change touches them. One or two plain-
sentence bullets per change is enough; this isn't meant to restate the
full commit message.

## 2026-09-09

- Added Windows desktop automation as a fourth `ToolProvider` arm
  (`windows_tools.py`, via pywinauto's UI Automation backend), scoped down
  exactly as the design decision recorded for it called for: launch-app +
  list/click/type/read-controls only, controls addressed by index from the
  most recent listing (mirrors the browser arm's `observe()` ->
  `click(index)` pattern), every mutating action R3 (always confirms, not
  configurable off). Off by default (`ENABLE_WINDOWS_AUTOMATION`).
  Validated against real windows (Notepad, Calculator) both directly and
  through a full end-to-end run of the actual agent loop, which surfaced
  three real bugs neither mocked tests nor pywinauto's docs would have
  caught: typing via UIA's ValuePattern (`set_edit_text`) silently
  corrupted text with no exception raised on a modern WinUI-based app,
  fixed by making `type_keys()` (real simulated keystrokes) the primary
  method instead; a control reference from `windows_list_controls` can go
  stale the moment the app updates that control in place, now documented
  in the tool descriptions so the model re-lists controls before reading
  anything a prior action may have changed; and clicking via `click_input()`
  (real synthetic mouse input at screen coordinates) silently did nothing
  whenever another window had focus between LLM-driven steps -- caught only
  by a full agent-driven run, not isolated manual calls -- fixed by using
  `invoke()` (UIA's InvokePattern) as the primary click method instead.
- Added an eval suite (`evals/`) that runs representative tasks against a
  real, configured LLM and scores what the agent actually did, complementing
  the mocked test suite which only proves the mechanism is correct.
- Added per-task LLM token usage tracking (input/output), surfaced in
  `LLMClient.get_usage()` and every `output/*.json` result record.
- Brought README.md and requirements.txt back in line with the actual
  project state after several features had outpaced the docs.
- Added read-only local filesystem access as a third MCP server, scoped to
  exactly one user-named directory.
- Added oscillation and no-progress-streak detection to the agent loop, on
  top of the existing exact-repeat stuck-loop guard.
- Made `scroll` actually page through a long page's text -- it had been a
  no-op for text extraction, silently truncating anything past the first
  `MAX_DOM_CHARS` characters.
- Added Brave Search as a second MCP server; fixed two real bugs
  (a masked-empty-result bug and an unhelpfully blank startup-timeout
  message) found while building it.
- Filled test-coverage gaps across the LLM providers' response parsing,
  `browser.py`, `config.py`, `excel_tools.py`, and the manual
  login-wall-resolution prompt.
- Fixed a real secret-redaction bug in `logger.py`: a capturing-group bug
  meant bare API keys were logged in full despite looking redacted.
- Removed a dead `press_enter()` method from `browser.py` found while
  expanding its test coverage.

## 2026-09-08

- Added `ARCHITECTURE_DECISIONS.md`, consolidating three rounds of external
  "should we rebuild this" research review into one standing reference.
- Formalized the `ToolProvider` interface and the R0-R3 risk-tier policy,
  replacing the old per-arm hardcoded confirmation logic.
- Added MCP (Model Context Protocol) as a third `ToolProvider`, starting
  with a read-only "fetch" server, off by default.
- Added structured JSON result contracts to `output/` (status, summary,
  artifacts, verification warnings) instead of a bare success string.
- Added test coverage for `discord_bot.py`; fixed a small `output_path`
  reporting gap found while writing it.

## 2026-09-07

- Added the Discord bot interface for running tasks remotely, with
  sensitive-action confirmation made pluggable so a non-terminal front end
  can ask its own way.

## 2026-09-06

- Added the Phase 1 local AI browser automation agent: the observe/decide/
  act loop, the Playwright-based browser arm, and the CLI entry point.
- Defaulted to Anthropic (Claude) as the LLM provider.
- Added manual resolution of login/CAPTCHA walls (a human can solve it in
  the visible Chrome window) instead of always failing the task outright.
- Fixed the manual-resolution pause to also cover the model's own
  self-reported `login_required` action, not just the heuristic detector.
- Rejected `finish` actions with an empty summary instead of silently
  accepting them.
- Gave empty-finish retries their own guard instead of colliding with
  stuck-loop detection.
- Switched from prompted free-text JSON output to native LLM tool-calling.
- Fixed two false-positive login-wall detection bugs: bare-word matching
  flagged ordinary nav "Log in" links, and any password-type input field
  was treated as proof of a login wall.
- Added an explicit VERIFY step to the agent loop; fixed a real safety bug
  (a declined confirmation being silently swallowed) found while building it.
- Fixed a VERIFY false-positive on checkbox/dropdown/input state changes,
  which don't change the URL or visible text but are still real progress.
- Shipped Phase 2: added the Excel arm (openpyxl) and generalized the loop
  from a browser-only script into a multi-arm orchestrator.
