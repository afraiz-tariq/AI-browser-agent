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

## 2026-09-24

- Voice push-to-talk no longer uses pynput: on the user's PC its keyboard
  hook received no key presses at all (`--keys` printed nothing), while the
  microphone and speech model worked (`--mic-test` heard "open notepad").
  Keys are now read by polling Windows' `GetAsyncKeyState` ~50x/s: no hook,
  no extra package, no admin rights. pynput is no longer needed.

- Voice troubleshooting after the first real try (holding the key did
  nothing): `python voice.py --keys` shows which keys the program sees,
  `--mic-test` checks the microphone + speech model without the keyboard, a
  microphone that fails to start now says so, and the harmless Hugging Face
  symlink warning is silenced.

- Second live hybrid run (Jev in Windows apps too): 11/11 passed, whole
  suite 101 s vs 169 s Claude-only; Calculator 24.5 s -> 13.3 s. See
  `evals/README.md`.

- Added the voice front-end (`python voice.py`): hold right Ctrl to speak a
  task, local speech-to-text (faster-whisper, audio never leaves the PC),
  results spoken via Windows SAPI, spoken confirmations where only a plain
  "yes"/"confirm" continues, and a stop key (F10). `run_task()` gained an
  optional `should_stop` checked before each step and before each action.
  Logic tested offline with fakes; the microphone/model/speaker path needs
  a real run on Windows.

- Jev now also decides steps inside Windows app windows (`DECIDER=hybrid`):
  click a control, type a span of the task into one, or re-read the window's
  controls, always by the exact title from the latest listing; password
  boxes are never targets. Jev is now only asked when the task is on a page
  or in a just-listed window (no more wasted calls after Excel/launch
  steps), can scroll pages that have no controls, and reuses one TypeSafe
  connection across tasks. Offline-tested; not yet measured live.

- First live claude-vs-hybrid comparison (Sonnet 5, 11 tasks, user's PC):
  hybrid 11/11 vs 10/11, total time -18%, decision wait -29%, Jev steps
  median 272 ms vs ~2.2 s for Claude, Jev cost ~$0.0007. See
  `evals/README.md`.

- Added the optional TypeSafe Jev decider (`jev.py`, `DECIDER=hybrid`, off by
  default): Jev picks browser click / type / scroll steps from the page's own
  elements in one request; Claude decides everything else and any step Jev
  is unsure of, can't express, or can't reach. Text to type is only ever a
  span of the user's own task. Jev choices go through the same risk tiers
  and confirmations. 27 offline tests with TypeSafe simulated. Not yet run
  against the live API.
- `observe()` now names elements by their `<label>` / `aria-labelledby`
  text: wrapped checkboxes previously read as `''`, so neither model could
  tell them apart.
- Two click-heavy eval tasks (`browser_settings_toggles`,
  `browser_trip_wizard`) whose pages emit a code from the real control
  states, for a fair claude-vs-hybrid comparison.

- Recorded the first live speed/cost baseline (claude-sonnet-5, user's PC):
  9/9 evals passed; median step = 2.3 s Claude decision + 53 ms page read +
  21 ms action; prompt cache served 84% of prompt tokens; ~$0.15 for the
  run. See `evals/README.md` and `evals/results/`.

- Anthropic calls now prompt-cache the system prompt + tool definitions
  (~2.6k tokens, ~3.8k with the Windows arm; identical every step), so steps
  after the first bill that part at ~0.1x input price. Token usage now also
  records cache reads/writes, since the SDK's `input_tokens` excludes cached
  tokens. Not yet measured live (needs an API key): check
  `cache_read_input_tokens` > 0 in `output/*.json` after a multi-step task.

- `observe()` now reads the whole page in one in-page snapshot instead of
  ~7 Playwright round trips per element: 41-59x faster on the generated
  benchmark pages (50 elements: 1,169 -> 29 ms; 200: 4,352 -> 74 ms; 500:
  10,807 -> 208 ms, median of 10, headless Chromium in the dev container).
  Output checked identical, old vs new, on all 12 test fixtures plus the
  benchmark pages and an edge-case page (display:contents, hidden/collapsed,
  closed <details>, zero-size). New `evals/bench_observe.py` reproduces the
  numbers offline.
- Every output record now has a `timings` block (per-step observe/decide/act
  ms, with human [y/n] wait excluded, plus medians), and
  `evals/run_evals.py` reports them and can `--save` a JSON baseline. Part of
  Phase 0 of `docs/JEV_VOICE_PLAN.md`; the live Claude baseline itself still
  needs a run with a real API key.

- Fixed secret field values being able to reach the LLM: a pre-filled
  password input with no label had its password used as its label in the
  prompt. Both arms now mask password/PIN/card/token fields (browser:
  `type=password`, autocomplete tokens, secret-sounding labels; Windows: UIA
  `IsPassword`) via the new shared `secret_fields.py`. See
  ARCHITECTURE_DECISIONS.md §1, bug 12.

- Added `docs/JEV_VOICE_PLAN.md`: a phased, additive plan for TypeSafe Jev decisions (with Claude escalation) and Windows voice control, based on a study of Rocky, jev-ultrafast, jev-voice and typesafe-computer-use. A proposal only; no code changed.

## 2026-09-09

- Fixed `windows_type_into_control` still producing corrupted text
  (`"hello world"` -> `"hello orld"`, a dropped `w`, or `"hello ddddd"`,
  garbled/repeated characters) even after switching to `type_keys()` --
  root cause was `set_focus()` returning before focus had actually
  settled, so the first keystroke(s) sent immediately after could be
  dropped, plus `type_keys()`'s default (unpaced) rate outrunning a
  busier app. Fixed with an explicit settling delay before typing and an
  explicit inter-keystroke pause. Verified with 5/5 clean back-to-back
  typing attempts against genuinely isolated targets, after discovering
  (the hard way) that this machine's Notepad is single-instance with
  tabs -- repeated `windows_launch_app` calls add tabs to one shared
  process rather than opening independent windows, which had been
  quietly confounding earlier repro attempts.
- Loosened the Windows automation arm's confirmation policy from "every
  mutating action always confirms" to dynamic, content-aware risk --
  mirroring the browser arm's existing pattern rather than inventing a new
  one. `windows_click_control` now uses `WindowsToolProvider.
  get_dynamic_risk()` (the same mechanism `BrowserToolProvider.
  get_dynamic_risk()`/`is_sensitive()` uses): R2 only if the resolved
  control's own UIA-read text matches a sensitive-keyword list
  (browser.py's list plus Windows-specific additions like `uninstall`/
  `format`/`shut down`), R0 otherwise. `windows_type_into_control` moved to
  R1 (confirms only with `CONFIRM_R1_ACTIONS` on -- typing is reversible,
  the risk is in whatever button gets pressed after). `windows_launch_app`
  moved to R2 (default-confirm, tunable off, was unconditional R3).
  `windows_close_window` stays R2 (no per-control text to judge a whole-
  window close by). This was possible once `windows_list_controls`
  existed to give real ground truth to judge risk by -- the same kind the
  DOM already gave the browser arm -- which didn't exist when the arm was
  first scoped as "confirm everything." 6 new tests for the dynamic-risk
  logic; verified live that Calculator's digit/operator clicks no longer
  prompt while a control with a sensitive-sounding name still does.
- Added a Windows automation eval task (`evals/tasks.py`): launch
  Calculator, compute 7 + 3 via its real UI controls, and report the
  result -- the same task the arm was manually verified against
  end-to-end while building it. Skips cleanly, like the MCP eval tasks,
  when `ENABLE_WINDOWS_AUTOMATION` is off.
- Added a GitHub Actions workflow (`.github/workflows/tests.yml`) that runs
  the offline test suite on every push/PR to `main` -- the project had no CI
  at all before this; every prior test run was manual.
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
  anything a prior action may have changed; clicking via `click_input()`
  (real synthetic mouse input at screen coordinates) silently did nothing
  whenever another window had focus between LLM-driven steps -- caught only
  by a full agent-driven run, not isolated manual calls -- fixed by using
  `invoke()` (UIA's InvokePattern) as the primary click method instead; and
  a fourth found on a real user's machine during their own first try: a
  short guessed window title (a word from text just typed, instead of the
  exact title from `windows_list_windows`) silently matched one wrong,
  unrelated window on a busy desktop with no ambiguity error, sending the
  model chasing a false "garbled typing" trail before it self-corrected by
  starting over. Fixed by trying an exact title match before falling back
  to substring matching, plus stronger tool-description guidance toward
  exact titles.
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
