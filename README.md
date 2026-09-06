# AI Browser Agent

A small, local, command-line AI agent that reads a plain-English task, uses
an LLM to decide what to do, and acts on it through one or more "arms" --
Chrome (via Playwright) and Excel (via openpyxl so far) -- until the task
is done.

**Status:** Phase 1 (the browser arm) is complete and has been validated
with real end-to-end runs -- search, multi-page research, form-filling with
the safety confirmation firing correctly. Phase 2 is in progress: the Excel
arm above is its first piece. Windows desktop automation and a phone/LAN
command interface are designed but not yet built (see **Phase 2 design**
below).

## Architecture

The agent is one orchestrator loop that can act through multiple arms, not
a browser-specific program with automation bolted on:

```
User (types a task at the prompt)
        |
        v
   agent.py              <- the observe/decide/act/verify loop lives here
        |
        v
     llm.py  <---------------------------------+
        |  (one flat list of tools from BOTH     |
        |   arms; the model picks exactly one)   |
        v                                         |
   ┌────┴─────┐                                   |
   v          v                                   |
browser.py  excel_tools.py                        |
   |          |                                   |
Playwright  openpyxl                              |
   |          |                                   |
Chrome ---> Website                                |
   |                                               |
   +----------------- result / observation --------+
```

Concretely, each step of a task is:

1. **Observe** -- if a browser page is open, `browser.py` asks Playwright
   for its URL, title, a numbered list of interactive elements (links,
   buttons, inputs -- using the DOM/accessibility tree, not a screenshot),
   and a snippet of visible text. The Excel arm has no equivalent
   "observe the whole environment" step -- its actions report their own
   result directly (see step 3).
2. **Decide** -- `llm.py` sends the task, a short history of what's been
   tried (including every past excel_* action's own result), and the
   browser observation (or a note that no page is open yet) to the
   configured LLM, exposing **every action from every arm as one flat list
   of native tools/functions** -- `goto`, `click`, `type`, `scroll`,
   `extract`, `finish`, `login_required` from the browser arm, and
   `excel_open`, `excel_read_cell`, `excel_read_range`, `excel_write_cell`,
   `excel_save`, `excel_list_sheets` from the spreadsheet arm. The model
   calls exactly one tool, from either arm, on each turn -- there's no
   separate "pick an arm first" step. Native tool calling also keeps
   required fields (like `finish` needing an actual, non-empty answer)
   enforced by the API itself rather than hoped for from free-text JSON.
3. **Act** -- `agent.py` dispatches that action to whichever arm owns it
   (by name: `excel_*` goes to `excel_tools.py`, everything else to
   `browser.py`). Browser actions that look like they submit a form, send
   something, or delete/purchase something, and `excel_save` (it
   overwrites a real file), all ask you `[y/n]` before running (see
   **Safety** below). Chrome itself is only launched the first time a
   browser action actually runs -- a pure "update this spreadsheet" task
   never touches it at all.
4. **Verify** -- once the *next* OBSERVE happens (step 1 again) for a
   browser action, the agent compares the page's URL, visible text, and
   every interactive element's state (checked/selected/value -- see
   `state_fingerprint` in `browser.py`) to what they were right before the
   action ran. If an action that's supposed to change the page (a
   navigation, a click, a submitted form) left all of that completely
   unchanged, that's flagged immediately in the action's own history entry
   -- e.g. `[VERIFY: no observable change after click ... -- it may not
   have worked]` -- so the model finds out on its very next decision
   instead of a human having to notice a stuck task several steps later.
   This needs no extra API calls (just comparing two observations the loop
   already made) and only a handful of cheap extra Playwright reads per
   step. Excel actions don't need this: they're deterministic and already
   report their own result directly in step 3. Including element state
   (not just URL/text) in the comparison matters in practice: an earlier
   version only checked URL and visible text, and a checkbox click --
   which changes neither -- convinced the model its own successful click
   had failed, sending it into a repeated-clicking spiral until it tripped
   the stuck-loop guard below.
5. Repeat, up to `MAX_STEPS` times, until the model returns `finish` (or
   the agent detects a login wall, a stuck loop, or a hard error).

Every step is written to a per-task log file, and the final answer is also
saved as JSON under `output/`.

This loop is intentionally implemented directly (rather than pulling in a
heavier agent framework) so each step is visible in a few hundred lines of
commented Python -- see `agent.py`, `browser.py`, `excel_tools.py`, and
`llm.py` for the actual mechanics.

## Phase 2 design

The plan discussed for extending this beyond the browser:

- **Flat tool dispatch across arms** (implemented, see above) rather than a
  two-level "pick an arm, then pick an action within it" -- a model
  choosing from a few dozen well-named tools works fine, and hierarchy
  would just add a round-trip for no benefit at this scale.
- **Excel via openpyxl for closed files** (implemented). `xlwings`/COM for
  reading a workbook the user already has open live in Excel is a
  deliberate scope cut, not forgotten -- it would be a second, separate
  tool this same arm could grow if a real task needs it.
- **Windows desktop automation is scoped down and comes later, not next.**
  General "understand and click any button in any Windows app" is far
  more open-ended and brittle than either the browser (a real DOM) or
  Excel (a real file format) -- there's no accessibility-tree equivalent
  as reliable as either. When it's built, it should start narrow (launch
  an app, handle known dialogs like Open/Save) rather than aiming for
  general-purpose UI understanding, and its `[y/n]` confirmation gate
  should probably default to *every* action needing confirmation (opt-out
  for a short allowlist), not the browser arm's opt-in keyword-matching --
  a Windows arm's blast radius (other apps' data, system dialogs) is
  bigger than a browser tab's.
- **Phone/LAN command interface, later still.** A local HTTP endpoint
  `agent.py` listens on, so a phone on the same Wi-Fi can submit a task
  and get the result back. No cloud exposure -- but even LAN-only should
  have a minimal shared-secret check, since "same Wi-Fi" still means any
  other device on it could otherwise hit the endpoint.

## Project structure

```
ai_browser_agent/
├── agent.py          # CLI entry point + the observe/decide/act/verify loop
├── browser.py         # Browser arm: Playwright wrapper (launch Chrome, observe page, run actions)
├── excel_tools.py      # Excel arm: openpyxl wrapper (open/read/write/save .xlsx files)
├── llm.py             # Provider-agnostic LLM client (OpenAI / Anthropic / mock); merges both arms' tools
├── logger.py           # Per-task plain-text logging (with secret redaction)
├── config.py           # Loads and validates .env settings
├── requirements.txt
├── .env.example
├── .gitignore
├── logs/               # One .log file per task run (gitignored)
├── output/             # One .json result file per successful task (gitignored)
└── tests/              # Offline tests (mock LLM + local fixture pages + tmp .xlsx files, no internet needed)
```

## Installation (Windows)

```
cd ai_browser_agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Install a Playwright-managed browser (used only as a fallback -- see
below for using your real, already-installed Chrome):

```
python -m playwright install chromium
```

The agent is configured by default to drive your **real, already-installed
Google Chrome** (via Playwright's `channel="chrome"` option), not a
separate Playwright-managed copy. If Chrome is installed in a
non-standard location, or you'd rather use the Playwright-managed
Chromium above, set `CHROME_EXECUTABLE_PATH` in `.env` accordingly, or
leave it blank and rely on `CHROME_CHANNEL=chrome` auto-detection.

Configure your `.env`:

```
copy .env.example .env
notepad .env
```

At minimum, set `ANTHROPIC_API_KEY` (the default `LLM_PROVIDER` is
`anthropic`; set it to `openai` and fill in `OPENAI_API_KEY` instead if
you'd rather use that). Everything else has a sensible default.

## Running

```
python agent.py
```

```
Task: Open Google and search for OpenAI.
```

You can also pass the task directly:

```
python agent.py "Open Google and search for OpenAI."
```

A Chrome window will open (set `HEADLESS=true` in `.env` to run without a
visible window once you trust it) and you'll see each step printed to the
terminal as it happens, e.g.:

```
Step 1: I see the Google search box, I'll type the query.
  -> type {'index': 3, 'text': 'OpenAI', 'submit': True}
```

When the task finishes, the final answer is printed and saved to
`output/<timestamp>.json`. A full log of the run is written to
`logs/<timestamp>.log`.

### Example tasks

Browser arm:

1. `Open Google and search for OpenAI.`
2. `Search Google for the latest information about RF impedance matching and summarize the top result.`
3. `Search Google for the top 5 companies developing RF plasma impedance matching systems and give me a short comparison.`
4. `Go to Wikipedia, search for "Playwright (software)", and save a two-sentence summary to a file.`
5. `Open https://news.ycombinator.com, find the top story, and tell me its title and score.`

Excel arm (paths are examples -- use a real path on your machine):

6. `Open C:\Users\me\Desktop\report.xlsx, read cell A1, and tell me what's in it.`
7. `Create a new spreadsheet at C:\Users\me\Desktop\test.xlsx with "Hello" in A1, then save it.`

Mixed (both arms in one task):

8. `Open C:\Users\me\Desktop\suppliers.xlsx, read the company name in A2, search for it on Google, and write a one-line summary of what you find into B2, then save.`

### Milestones (recommended order to test in)

1. **Milestone 1**: `Open Google and search for OpenAI.` -- confirms the
   whole loop (launch Chrome, search, read results, summarize, save)
   works before trying anything harder.
2. **Milestone 2**: a multi-result research task, e.g. task #3 above --
   confirms the agent can visit multiple pages and combine information.
3. **Milestone 3**: point it at any other website + a task, e.g.
   `Open <url>, find <information>, and save the result.` -- confirms
   nothing about the implementation is hard-coded to Google; the task and
   URL are just plain text typed at the prompt.
4. **Milestone 4** (Phase 2, Excel arm): task #6 or #7 above -- confirms
   the Excel arm works on its own, and that a pure-Excel task never even
   launches Chrome (watch: no browser window should open).
5. **Milestone 5** (Phase 2, mixed arms): task #8 above -- confirms the
   orchestrator can move between arms within a single task and that data
   read from one arm can be used in an action on the other.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `anthropic`, `openai`, or `mock` (mock is for tests only) |
| `LLM_MODEL` | Model name for that provider, e.g. `claude-sonnet-5` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Your API key, never hard-coded |
| `MAX_STEPS` | Hard cap on observe/decide/act/verify cycles per task (cost control) |
| `STEP_TIMEOUT_MS` | Playwright timeout per page load/action |
| `MAX_DOM_CHARS` | How much page text is sent to the LLM per step (cost control) |
| `HEADLESS` | `false` shows the Chrome window (recommended while learning) |
| `CHROME_CHANNEL` | `chrome` = drive your real installed Chrome |
| `CHROME_EXECUTABLE_PATH` | Override if Chrome is in a non-standard location |
| `CHROME_USER_DATA_DIR` | Persistent profile folder, so manual logins carry over between runs |
| `USE_PERSISTENT_PROFILE` | `true` keeps cookies/logins between runs |
| `CONFIRM_SENSITIVE_ACTIONS` | `true` asks `[y/n]` before risky actions |

Changing `LLM_PROVIDER`/`LLM_MODEL` is the only thing needed to switch
models later -- nothing else in the code references a specific provider.

## Safety

- The agent **never** attempts to bypass login, CAPTCHA, MFA, rate limits,
  or other access controls. If a page looks like a login/verification
  wall, the agent stops and tells you to log in manually (in the same
  persistent Chrome profile it uses, so the login is then remembered).
  If `HEADLESS=false` (so you can see the Chrome window), it will instead
  pause and let you resolve the wall yourself right there -- solve the
  CAPTCHA or log in, then press Enter in the terminal to let the agent
  continue from where it left off, instead of restarting the whole task.
- Before any action that looks like it submits a form, sends a
  message, makes a purchase, deletes something, or changes account
  settings, the agent asks:
  ```
  Ready to click <button 'Submit'>. This looks like it may have side effects. Continue? [y/n]
  ```
  Declining stops the task immediately with an explanation.
- `excel_save` gets the same treatment -- it's the only Excel action that
  touches disk (reads and `excel_write_cell` only change the in-memory
  workbook), so it always asks before overwriting a real file:
  ```
  Ready to save the workbook to 'C:\...\report.xlsx', overwriting it. Continue? [y/n]
  ```
- No password, API key, cookie, or session token is ever written to a log
  file (`logger.py` also redacts anything that looks like a secret as a
  defense in depth).

## Cost awareness

- `MAX_STEPS` hard-caps how many LLM calls a single task can make.
- Only a compact, text-only observation (not a screenshot, not full HTML)
  is sent per step, capped at `MAX_DOM_CHARS` characters.
- A short action history (last 8 steps) is included so the model has
  context without resending the whole conversation.
- If the model repeats the exact same action 3 times in a row, the agent
  assumes it's stuck and stops rather than burning further API calls.

## Error handling

Every failure mode prints and logs three things: **what** happened,
**why** it likely happened, and **what you can do** about it. Handled
cases include: Chrome failing to launch, a page that won't load, an
element index that no longer exists, an LLM/API error, a detected login
wall, and a task that doesn't finish within `MAX_STEPS`.

## Troubleshooting

- **`ANTHROPIC_API_KEY is not set`** (or `OPENAI_API_KEY`, if you switched
  providers) -- copy `.env.example` to `.env` and add your key.
- **Chrome doesn't launch / "executable doesn't exist"** -- make sure
  Google Chrome is installed, or set `CHROME_EXECUTABLE_PATH` in `.env` to
  its `chrome.exe` path, or run `python -m playwright install chromium`
  and remove `CHROME_CHANNEL`/set `CHROME_EXECUTABLE_PATH` accordingly.
- **Agent keeps saying login is required on a site you're already logged
  into in normal Chrome** -- Phase 1 uses its *own* persistent profile
  (`CHROME_USER_DATA_DIR`), separate from your everyday Chrome profile, so
  it starts logged out everywhere. Run the agent once, let it open the
  site, and log in manually in that window -- it'll be remembered next
  time.
- **"The agent repeated the same action three times without progress"**
  -- the model got stuck; try rephrasing the task more specifically.
- **Task fails with "did not finish within MAX_STEPS"** -- either raise
  `MAX_STEPS` in `.env`, or split the task into smaller ones.
- **Model reply is missing valid JSON** -- rare, but can happen with very
  small/local models; try a more capable model in `LLM_MODEL`.

## Running the tests

The test suite runs entirely offline against local fixture pages
(`tests/fixtures/`) using a scripted `mock` LLM provider, so it needs no
API key and no internet access:

```
pip install pytest
pytest tests/ -v
```

## What Phase 1 deliberately does *not* do

No web UI, no mobile app, no database, no multi-agent system, no
always-on background process, no Windows desktop control, no long-term
memory, no CAPTCHA/MFA bypass, no Docker. Those are candidates for a
later phase, once this browser-only prototype has proven reliable.
