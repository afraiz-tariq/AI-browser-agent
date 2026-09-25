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

## 2026-09-25

- From the exe's first real runs on the user's PC:
  - The voice app keeps Chrome open between tasks (`KEEP_BROWSER_OPEN`,
    default on): a YouTube video a task started was closed when the task
    finished. A Chrome the person closed is replaced on next use.
  - `windows_launch_app` now says which window appeared (or which already-open
    window the app reused), and repeating a look-only action (list windows,
    list controls, read, extract) a third time in a row gets a "not run --
    act on the result" hint before the stuck-loop guard stops the task. The
    Notepad run had listed windows three times and been stopped.

- Exe, first real run on the user's PC: the downloaded app crashed on start
  with "Failed to resolve Python.Runtime.Loader.Initialize" -- Windows marks
  unzipped files as downloaded, and .NET won't load the app window's DLL
  from them. The exe now removes that mark from its own bundled files on
  start, falls back to the small window (instead of crashing) if the app
  window's backend still can't load, and `--check-install` checks that
  backend. The download is now one zip, not a zip inside a zip.

- Fix for the exe: Playwright's browser driver (a Node.js program inside
  the playwright package) wasn't bundled -- PyInstaller has no rule for it
  and the import check still passed -- so browser tasks would have failed in
  `AI Agent.exe`. `ai_agent.spec` now collects `playwright` in full, and
  `--check-install` (run by the build job) fails if the driver is missing.

- Double-click app: `AI Agent.exe` (PyInstaller, `ai_agent.spec` /
  `build_exe.bat`, or the *Build Windows app* GitHub action, which builds
  and checks it on Windows). `.env`, logs and output live next to the exe
  (`app_paths.py`); the first run creates `.env` from `.env.example`;
  `voice.py --check-install` reports which parts of the app load.
- Windows arm, after comparing with ChatGPT's computer use on the same
  Notepad task: a `windows_press_keys` tool (Ctrl+N for a new tab, Tab, Esc,
  ...), each shortcut classified R0-R3 with unlisted ones always asking;
  every click, type and key press now returns the window's fresh control
  list, saving a step per action; app tips in the prompt (Notepad: Ctrl+N
  for a new empty tab). Fixes the "open new notepad and write hello" loop.

- The app window (`app_ui.py`, `ui/`, pywebview): a chat-style feed with
  every step, approval cards, Pause / Take over / Stop, history of past
  tasks, compact always-on-top mode. Two-way: messages typed mid-task steer
  the running task (added to the TASK text, the trusted slot), the agent
  can ask you a question (new `ask_user` tool, R0, `user_tools.py`), and a
  login wall hands over with a Continue button. The Windows arm now refuses
  to list or touch the agent's own windows. Falls back to the small
  tkinter window without pywebview.

- Type a task instead of saying it: a text box in the voice window (and
  typing in the terminal) runs it through the same path as a spoken task,
  confirmations included. Typing "yes"/"no" answers an open question. A
  typed task is refused, not queued, while one is running.

- Voice looks like an app now: `start_voice.bat` runs it without a
  terminal, as a small floating window (what it heard, the current step,
  the result) and an icon by the clock (state colour; Show window, Pause
  microphone, Open log, Quit). Confirmations show Yes / No buttons next to
  the spoken question -- a click or a spoken answer, whichever comes
  first. `run_task()` gained an optional display-only `on_step` callback.
  `VOICE_UI=false` keeps the terminal.

- Less effort to use voice: tap right Ctrl and speak (recording ends about
  a second after you stop talking; `VOICE_MODE=hold` keeps the old
  hold-to-talk), spoken yes/no answers end as soon as you've said them,
  `start_voice.bat` runs it without a terminal or activating `.venv`, and
  `python voice.py --autostart on` starts it minimized at every login. A
  second copy refuses to start, so a task never runs twice.

- A voice "search Google for APC" stopped at "Ready to type into
  <textarea> '검색' and submit" and then read silence as a "no". A plain
  search (a GET search form) is now R1, so it no longer asks; the voice
  confirm asks once more on silence (listening 5 s, not 4) instead of
  declining, and the failure message no longer claims "User declined".
  Chrome's translate pop-up is turned off (in the agent's own Chrome
  profile). Follow-up the same day: Google still asked, most likely because
  its search form holds a hidden file field (search by image), which the
  first check wrongly treated as "not just a search"; fixed.

- "Take a screenshot" works now. A voice run drove the Snipping Tool and
  spent all 20 steps on its capture overlay, which waits for a mouse drag
  the Windows arm can't do. New `windows_screenshot` tool (R1: a new PNG in
  `output/screenshots/`), a matching voice quick command, and a prompt rule
  steering the model away from the Snipping Tool. The stuck-loop guard now
  also stops a three-action cycle repeated twice (that run's
  click / list windows / list controls loop).

- DeepSeek (`deepseek-flash`) is now the default in `.env.example`, by the
  user's choice after it matched Sonnet on the evals at ~10% of the cost.
  Anthropic stays supported (ARCHITECTURE_DECISIONS.md §2a).

## 2026-09-24

- DeepSeek measured (2026-09-25, `deepseek-flash`, hybrid): 11/11 evals
  passed, 81 s vs Sonnet's 101 s, ~$0.012 vs ~$0.114 for the run. See
  `evals/README.md`.

- Corrected the documented DeepSeek model name to `deepseek-flash` (the API
  rejected `deepseek-v4.1-flash`, listing `deepseek-flash`, `deepseek-v4-pro`).
  An unknown model name now gets its own plain explanation and stops the
  eval run early; the eval report says "the LLM" rather than "Claude".

- First DeepSeek eval: the key worked but the account had no balance (402)
  on every task. The eval runner now stops at the first no-credit /
  rejected-key failure, and OpenAI-compatible errors name the real host
  (e.g. `api.deepseek.com`) instead of just "OpenAI".

- Cheaper LLM providers: `LLM_PROVIDER=deepseek`, `gemini` or `openrouter`
  (plus `OPENAI_BASE_URL` for any OpenAI-compatible server), via the
  existing OpenAI provider. A setting a model rejects (e.g. GPT-5's
  temperature) is dropped and the request retried; DeepSeek's thinking mode
  is off; cached tokens are counted the same way for every provider. README
  "Cost awareness" has the measured per-step cost comparison.

- LLM failures are explained in plain words: out of credit (with where to
  add it), rejected key, service busy. An empty Anthropic credit balance
  had been reported as "could not be reached ... check your internet".

- Voice mode now prints a failed task's full explanation on screen; a
  failed LLM call had only been spoken as "could not be reached", hiding
  the actual API error.

- Re-run on the user's PC after the bug-13 fixes: "open notepad and write
  hello world" now really types and verifies; "sum 3 plus 2" took 5 steps
  (one `windows_click_controls` for 3, +, 2, =) instead of 16 and answered
  "3 plus 2 is 5." Follow-up: `windows_type_into_control` now reports the
  read-back text and a renamed window title (typing renamed "Untitled -
  Notepad" to "*hello world - Notepad", costing two extra steps to re-find
  it); pywinauto's harmless STA COM warning is silenced in voice.py.

- Fixes from the first voice runs (ARCHITECTURE_DECISIONS.md §1, bug 13):
  the model must now report only what this task itself did (Notepad's
  restored "Hello World" tab had been claimed as done); new
  `windows_click_controls` presses a known sequence (3, +, 2, =) in one
  step; Jev's floor in app windows is 0.8 (its wrong Calculator picks were
  at ~0.6); spoken replies are one short sentence.

- Voice quick commands (`quick_commands.py`): "open notepad", "go to
  youtube", "search youtube for ...", "volume up", "pause", "next track"
  and similar one-step requests run directly in well under a second
  instead of a full agent run (~5 s). An exact-phrase matcher first, then
  one Jev request for other phrasings (>= `QUICK_MIN_CONFIDENCE`);
  anything else runs the full agent. Apps open only if the Windows arm's
  own risk check says R0 (`SAFE_APPS`); URLs are built by code; media keys
  classified R0 explicitly.

- First working voice run ("open notepad": heard, launched, answered).
  At the user's request, opening a safe-listed app (`SAFE_APPS`, default
  Notepad/Calculator/Paint/Snipping Tool/Explorer) by bare name with no
  arguments no longer asks `[y/n]`; any path, arguments or other app still
  does. Voice now says it's listening for a yes/no (no key needed) instead
  of "still working" when the key is pressed during a confirmation.

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
