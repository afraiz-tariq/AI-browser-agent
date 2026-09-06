# AI Browser Agent (Phase 1)

A small, local, command-line AI agent that reads a plain-English task,
uses an LLM to decide what to click/type/read, and drives Google Chrome
via Playwright until the task is done.

Phase 1 is deliberately narrow: it only automates Chrome (no desktop
control, no GUI, no database, no multi-agent orchestration). The goal is a
prototype you can actually run today and understand end-to-end.

## Architecture

```
User (types a task at the prompt)
        |
        v
   agent.py            <- the observe/decide/act/verify loop lives here
        |
        v
     llm.py  <-------------------------+
        |  (asks: "given this page,     |
        |   what should I do next?")    |
        v                                |
   browser.py  ----> Playwright ----> Chrome ----> Website
        |                                          |
        +------------- observation ----------------+
        (URL, title, list of clickable/typeable elements, visible text)
```

Concretely, each step of a task is:

1. **Observe** -- `browser.py` asks Playwright for the current page's URL,
   title, a numbered list of interactive elements (links, buttons, inputs
   -- using the DOM/accessibility tree, not a screenshot), and a snippet of
   visible text.
2. **Decide** -- `llm.py` sends the task, a short history of what's been
   tried, and that observation to the configured LLM, exposing each possible
   action (`goto`, `click`, `type`, `scroll`, `extract`, `finish`,
   `login_required`, ...) as a native tool/function call. The model must
   call exactly one, which is what keeps required fields (like `finish`
   needing an actual, non-empty answer) enforced by the API itself rather
   than hoped for from free-text JSON.
3. **Act** -- `agent.py` executes that action through `browser.py`. Actions
   that look like they submit a form, send something, or delete/purchase
   something first ask you `[y/n]` before running (see **Safety** below).
4. **Verify** -- once the *next* OBSERVE happens (step 1 again), the agent
   compares the page's URL and visible text to what they were right before
   the action ran. If an action that's supposed to change the page (a
   navigation, a click, a submitted form) left both completely unchanged,
   that's flagged immediately in the action's own history entry -- e.g.
   `[VERIFY: no observable change after click ... -- it may not have
   worked]` -- so the model finds out on its very next decision instead of
   a human having to notice a stuck task several steps later. This needs no
   extra API or Playwright calls: it's just comparing two observations the
   loop already made. (A click that only toggles something like a
   checkbox's checked state, without changing the URL or visible text, is
   a known false-negative here -- acceptable for a prototype-level signal.)
5. Repeat, up to `MAX_STEPS` times, until the model returns `finish` (or
   the agent detects a login wall, a stuck loop, or a hard error).

Every step is written to a per-task log file, and the final answer is also
saved as JSON under `output/`.

This loop is intentionally implemented directly (rather than pulling in the
`browser-use` package) so each step is visible in ~250 lines of commented
Python -- see `agent.py`, `browser.py`, and `llm.py` for the actual
mechanics.

## Project structure

```
ai_browser_agent/
├── agent.py          # CLI entry point + the observe/decide/act/verify loop
├── browser.py         # Playwright wrapper: launch Chrome, observe page, run actions
├── llm.py             # Provider-agnostic LLM client (OpenAI / Anthropic / mock)
├── logger.py           # Per-task plain-text logging (with secret redaction)
├── config.py           # Loads and validates .env settings
├── requirements.txt
├── .env.example
├── .gitignore
├── logs/               # One .log file per task run (gitignored)
├── output/             # One .json result file per successful task (gitignored)
└── tests/              # Offline tests (mock LLM + local fixture pages, no internet needed)
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

1. `Open Google and search for OpenAI.`
2. `Search Google for the latest information about RF impedance matching and summarize the top result.`
3. `Search Google for the top 5 companies developing RF plasma impedance matching systems and give me a short comparison.`
4. `Go to Wikipedia, search for "Playwright (software)", and save a two-sentence summary to a file.`
5. `Open https://news.ycombinator.com, find the top story, and tell me its title and score.`

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
