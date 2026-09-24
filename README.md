# AI Browser Agent

A small, local, personal AI agent that reads a plain-English task, uses an
LLM to decide what to do, and acts on it through one or more "arms" --
Chrome (via Playwright), Excel (via openpyxl), three optional MCP-based
arms (web fetch, Brave Search, local filesystem read access), and Windows
desktop automation (via pywinauto, opt-in) -- until the task is done.
Controllable from the command line or remotely via a Discord bot.

**Status:** the browser and Excel arms, the Discord bot, all three MCP
arms, and the Windows desktop automation arm are built and validated with
real end-to-end runs. Every arm implements a common `ToolProvider` contract
(see **Architecture** below) with a four-tier risk model (R0 read-only
through R3 always-confirm) governing which actions ask for `[y/n]`
confirmation before running. Every real LLM call's token usage (input/
output) is tracked per task and surfaced in both the structured output
record and `LLMClient.get_usage()`. 389 automated tests, fully offline,
plus a separate eval suite (`evals/`) that runs representative tasks
against a real configured LLM and scores what the agent actually did.

## Architecture

The agent is one orchestrator loop that can act through multiple arms, not
a browser-specific program with automation bolted on:

```
User (CLI prompt, or a Discord DM via discord_bot.py)
        |
        v
   agent.py                    <- the observe/decide/act/verify loop lives here
        |
        v
     llm.py  <-------------------------------------------------------+
        |  (one flat list of tools from EVERY arm the task has         |
        |   enabled; the model picks exactly one)                     |
        v                                                               |
   ┌────────┬──────────┬─────────────────────────┬──────────────┐      |
   v        v          v                         v              v      |
browser.py  excel_tools.py               mcp_tools.py       windows_tools.py
   |          |             ┌────────────┼────────────┐    (optional, opt-in)
Playwright  openpyxl        v            v            v          |
   |          |          fetch      Brave Search  filesystem   pywinauto
Chrome --> Website     (web page)  (web search)  (one local folder)  |
   |                       |            |             |          any Windows app
   +---------- result / observation ----------------------------------+
```

Every arm implements the same `ToolProvider` contract (`tool_provider.py`)
regardless of which box above it lives in -- `agent.py`'s loop dispatches
by tool name through one small registry, never by checking which arm it
came from.

Concretely, each step of a task is:

1. **Observe** -- if a browser page is open, `browser.py` asks Playwright
   for its URL, title, a numbered list of interactive elements (links,
   buttons, inputs -- using the DOM/accessibility tree, not a screenshot),
   and a chunk of visible text (`MAX_DOM_CHARS` characters, 6000 by
   default). A page longer than that isn't truncated and forgotten --
   `scroll` pages through the rest of the text on each following
   observation (independent of any real visual scroll position), and the
   model is told explicitly when there's more to read so it doesn't
   answer from a partial page or conclude something is absent just
   because it wasn't in the first chunk. The Excel arm has no equivalent
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
3. **Act** -- `agent.py` looks up which `ToolProvider` owns the chosen
   action (a small registry built from each arm's `get_tool_specs()`, see
   `tool_provider.py`) and dispatches to it. Each tool has a risk tier
   (R0-R3); browser actions that look like they submit a form, send
   something, or delete/purchase something, and `excel_save` (it
   overwrites a real file), are tier R2 and ask you `[y/n]` before running
   (see **Safety** below). Chrome itself is only launched the first time a
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
   the agent detects a login wall, a stuck loop, or a hard error). "Stuck"
   isn't just the same action repeated three times running -- the agent
   also catches oscillation (bouncing between two actions, e.g. scroll
   down/up/down/up, without a third option in between) and a run of
   several *different* actions that each individually had no observable
   effect (which neither the exact-repeat nor the oscillation check would
   catch on their own). The latter first nudges the model with a hint to
   try something different before giving up a couple of steps later --
   see `CONSECUTIVE_NO_EFFECT_*` in `agent.py`.

Every step is written to a per-task log file, and a structured result
record is also saved as JSON under `output/` -- for every run, not just
successful ones (status, summary, how many steps it took, which URLs/files/
MCP tools it actually touched, and any VERIFY warnings raised along the way).

This loop is intentionally implemented directly (rather than pulling in a
heavier agent framework) so each step is visible in a few hundred lines of
commented Python -- see `agent.py`, `browser.py`, `excel_tools.py`, and
`llm.py` for the actual mechanics.

### MCP arm (optional, off by default)

A third arm, `mcp_tools.py`, wraps an [MCP](https://modelcontextprotocol.io)
server as a `ToolProvider` -- additive, not a replacement for the browser/
Excel arms. Each server it wires up is off by default and needs its own
opt-in flag:

- **fetch** -- the official read-only MCP reference server (HTTP GET +
  HTML-to-text extraction), exposed as `mcp_fetch`. `ENABLE_MCP_FETCH=true`
  after `pip install mcp mcp-server-fetch`.
- **Brave Search** -- web/local/video/image/news search plus a summarizer,
  via Brave's own actively-maintained `@brave/brave-search-mcp-server`
  (deliberately not `@modelcontextprotocol/server-brave-search`, the same
  "official reference server" family as fetch -- that one is marked
  deprecated on npm). Exposed as `mcp_brave_web_search`,
  `mcp_brave_local_search`, etc. Needs Node.js (run via `npx`, the same
  real-world dependency fetch's bundled Readability engine already has)
  and a free API key from https://brave.com/search/api/.
  `ENABLE_MCP_BRAVE_SEARCH=true` plus `BRAVE_API_KEY=...`.
- **Filesystem** -- read/list/search files in exactly one local folder you
  name, via the official `@modelcontextprotocol/server-filesystem`.
  Exposed as `mcp_read_text_file`, `mcp_list_directory`, `mcp_search_files`,
  etc. There's deliberately no default folder: `MCP_FILESYSTEM_ROOT` names
  the one directory you're comfortable exposing, and the server itself
  rejects any path outside it as a second line of defense on top of this
  codebase's own risk tiers. Read-only tools are R0; the server's own
  write/edit/create-directory/move tools are deliberately left unclassified
  (R3, always confirm -- see below) rather than silently trusted just
  because they came bundled with the read tools. `ENABLE_MCP_FILESYSTEM=true`
  plus `MCP_FILESYSTEM_ROOT=/path/you/choose`.

Risk classification for MCP tools is done by this codebase, never taken
from the server's own tool description -- see `mcp_tools.py`'s
`FETCH_SERVER_RISK_OVERRIDES` / `BRAVE_SEARCH_RISK_OVERRIDES` /
`FILESYSTEM_RISK_OVERRIDES`. Any tool an MCP server exposes that isn't
explicitly reviewed there defaults to tier R3 (always confirm, not
configurable off), so a server update that silently adds a new or
dangerous tool -- or, as with the filesystem server, ships some genuinely
dangerous tools alongside safe ones in the very same package -- can't
skip confirmation just because the server calls it safe. Adding a further
MCP server means adding one more small factory function like
`build_fetch_provider()`/`build_brave_search_provider()`/
`build_filesystem_provider()`, not changing `MCPToolProvider` itself --
which is also how an API key gets to a server that needs one: as an
environment variable passed to just that subprocess (`MCPToolProvider`'s
`env` argument), not a CLI argument that would show up in a local process
listing.

### Windows desktop automation arm (optional, off by default)

A fourth arm, `windows_tools.py`, drives Windows applications via
[pywinauto](https://pywinauto.readthedocs.io/)'s UI Automation backend
(`backend="uia"`) -- a real accessibility tree, the same reliability class
as Playwright reading the DOM, not blind pixel/coordinate clicking. Off by
default (`ENABLE_WINDOWS_AUTOMATION=false`); Windows-only, and `config.py`
refuses to start a task with it enabled on any other OS.

Deliberately narrow scope, per the design decision this implements (see
`ARCHITECTURE_DECISIONS.md`): launch-app + list/click/type/read-controls
only, not a general "understand and control any Windows app" tool.
Controls are addressed by index from the most recent `windows_list_controls`
call for that window -- mirrors the browser arm's `observe()` ->
`click(index)` pattern exactly, never a name/selector the model guesses.

- `windows_launch_app(path, args)` -- R2, except a plain launch of a safe-listed app (`SAFE_APPS`: bare name, no args), which is R0.
- `windows_list_windows()` -- R0.
- `windows_list_controls(window_title)` -- R0. **Call this again after any
  click/type action, before reading a control affected by it** -- some
  apps replace a control's underlying element when its content changes
  (found on a calculator's result display after clicking `=`), so a
  reference from before the action can report stale, pre-action text.
- `windows_click_control(window_title, index)` -- **dynamic**: R2 if the
  target control's own text matches a sensitive-keyword list, R0 otherwise.
- `windows_click_controls(window_title, indices)` -- several clicks in one step, in order (e.g. Calculator 3, +, 2, =); **dynamic** like a single click: R2 if any control in the sequence looks sensitive, else R0.
- `windows_type_into_control(window_title, index, text)` -- R1. Reports the text read back from the control (never for password boxes) and the window's new title if typing renamed it (e.g. `*hello world - Notepad`).
- `windows_read_control_text(window_title, index)` -- R0.
- `windows_screenshot(window_title?)` -- R1. Saves the whole screen (or one window) as a new PNG in `output/screenshots/`; never overwrites, never sent anywhere. Needs `pip install pillow`. The model is told not to use the Snipping Tool: its capture overlay waits for a mouse drag this arm can't do.
- `windows_close_window(window_title)` -- R2.

Confirmation policy mirrors the browser arm's exactly, not a separate
scheme: `windows_click_control`'s risk is decided by
`WindowsToolProvider.get_dynamic_risk()`, the same mechanism
`BrowserToolProvider.get_dynamic_risk()` uses via `is_sensitive()` --
`windows_list_controls` already reads each control's real accessible text
via UI Automation, the same kind of ground truth the DOM gives the browser
arm, so a click only confirms when the target's own text matches a
keyword list (browser.py's list plus Windows-relevant additions:
`uninstall`, `format`, `erase`, `reset`, `wipe`, `shut down`, `restart`,
`sign out`). `windows_type_into_control` is R1 (confirms only if
`CONFIRM_R1_ACTIONS` is on) since typing is reversible -- the risk lives in
whatever button gets pressed afterward. `windows_launch_app` and
`windows_close_window` stay R2 (default-confirm, tunable off): there's no
control-text signal to judge a whole-app-launch or whole-window-close by.
The one exception: launching an app on the `SAFE_APPS` list (default:
Notepad, Calculator, Paint, Snipping Tool, File Explorer) by its bare name
with no arguments doesn't ask -- opening one changes nothing by itself. A
folder path (a look-alike `notepad.exe` elsewhere) or any arguments still
ask; set `SAFE_APPS=` empty to be asked before every launch.
This started as "every mutating action always confirms, not configurable
off" -- the right conservative starting point before `windows_list_controls`
existed to give real ground truth to judge risk by -- and was loosened
once that ground truth existed, the same way the browser arm already
worked. A launched app is deliberately left running when the task ends
rather than force-closed, since that could destroy the user's unsaved
work in it.

Five things found only by testing against real windows -- some by isolated
manual calls, others only by a full end-to-end run of the actual agent
loop (including several on a real user's own machine, not just this
project's own dev environment) -- not from pywinauto's docs alone (see
`windows_tools.py`'s module docstring and `CHANGELOG.md`): typing via
UIA's `ValuePattern` (`set_edit_text`) silently wrote corrupted text into
a modern WinUI-based app's control with no exception raised, so
`type_keys()` (real simulated keystrokes) is the primary method instead;
the stale-control-reference issue documented above; clicking via
`click_input()` (real synthetic mouse input at screen coordinates)
silently did nothing whenever another window had focus between LLM-driven
steps -- exactly the "blind pixel/coordinate clicking" this arm is meant
to avoid -- so `invoke()` (UIA's InvokePattern) is the primary click
method instead; a short guessed `window_title` (e.g. a word from text
just typed) could silently match one wrong, unrelated window on a busy
desktop with no ambiguity error -- fixed by trying an exact title match
first, falling back to substring matching only when no exact match
exists, plus stronger guidance in the tool descriptions to always pass
the exact title from `windows_list_windows`; and even with `type_keys()`
as the fix for the first bug, typing could still drop or garble
characters (`"hello world"` -> `"hello orld"` or `"hello ddddd"`) because
`set_focus()` returns before focus has actually settled and `type_keys()`
fired immediately after can lose its first keystroke(s) -- fixed with an
explicit settling delay and inter-keystroke pause, verified with 5/5
clean repeated attempts. `pip install
pywinauto` to turn this arm on.

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
- **Windows desktop automation** (implemented, see above). Scoped down
  exactly as originally decided: launch-app + list/click/type/read-controls
  only, not general-purpose UI understanding. Confirmation policy started
  as "every mutating action always confirms, not configurable off" -- the
  right conservative starting point before `windows_list_controls` existed
  to give real ground truth to judge risk by -- and was loosened once that
  ground truth existed: `windows_click_control` now uses the same dynamic,
  control-text-based risk tiering as the browser arm's clicks (see
  **Windows desktop automation arm** above), while `windows_launch_app`/
  `windows_close_window` still default-confirm, since a whole-app-launch
  or whole-window-close has no per-control text to judge risk by the way
  a click does.
- **Remote command interface** (implemented, see below) -- but as a
  **Discord bot** (`discord_bot.py`), not the local HTTP/LAN server
  originally sketched here. The bot makes an outbound connection to
  Discord, so there's nothing exposed on this PC at all (no port
  forwarding, no firewall rules, no VPN), and it works from anywhere with
  the Discord app and internet, not just the same Wi-Fi. It also lets
  sensitive-action confirmations be genuinely interactive -- the bot asks
  `y/n` in the chat and waits for a reply -- rather than an HTTP request's
  only realistic option of auto-declining every sensitive action outright.
  Getting there required making `run_task()`'s `[y/n]` confirmation
  mechanism pluggable (a `confirm_callback` parameter, defaulting to the
  original terminal `input()` prompt) instead of hard-coded, so a non-
  terminal front-end can ask its own way -- see `agent.py`.

## Project structure

```
ai_browser_agent/
├── agent.py          # CLI entry point + the observe/decide/act/verify loop
├── discord_bot.py     # Discord bot interface: calls run_task() with a chat-based confirm_callback
├── voice.py           # Voice interface: push-to-talk, local speech-to-text, spoken results and confirmations
├── quick_commands.py  # Voice shortcut: one-step commands (open app/site, volume, media) without a full agent run
├── browser.py         # Browser arm: Playwright wrapper (launch Chrome, observe page, run actions) + BrowserToolProvider
├── excel_tools.py      # Excel arm: openpyxl wrapper (open/read/write/save .xlsx files) + ExcelToolProvider
├── mcp_tools.py        # MCP arm (optional): wraps an MCP server (e.g. the "fetch" server) as a ToolProvider
├── windows_tools.py     # Windows desktop automation arm (optional): pywinauto (UI Automation) as a ToolProvider
├── tool_provider.py    # ToolProvider/ToolSpec contract every arm implements, and the R0-R3 risk-tier policy
├── errors.py           # Shared TaskCannotBeCompleted exception + explain() formatter
├── secret_fields.py    # Which form fields hold secrets, so both arms mask their values before the LLM sees them
├── llm.py             # Provider-agnostic LLM client (OpenAI / Anthropic / mock); builds tools from ToolSpecs
├── jev.py             # Optional TypeSafe Jev decider for browser steps (DECIDER=hybrid), Claude as fallback
├── logger.py           # Per-task plain-text logging (with secret redaction)
├── config.py           # Loads and validates .env settings
├── requirements.txt
├── .env.example
├── .gitignore
├── CHANGELOG.md         # Dated record of what shipped and when
├── ARCHITECTURE_DECISIONS.md  # Standing reference for why things are built this way
├── logs/               # One .log file per task run (gitignored)
├── output/             # One structured .json result record per task run, success or failure (gitignored)
├── tests/              # Offline tests (mock LLM + local fixture pages + tmp .xlsx files, no internet needed)
└── evals/              # Eval suite: real tasks run against a real configured LLM -- see evals/README.md
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

**Optional: the MCP arms.** All three (`ENABLE_MCP_FETCH`,
`ENABLE_MCP_BRAVE_SEARCH`, `ENABLE_MCP_FILESYSTEM` -- see **MCP arm**
above) are off by default; nothing below is needed unless you turn one on.
- `pip install mcp` is required for any of the three.
- Fetch also needs `pip install mcp-server-fetch`.
- Brave Search and Filesystem are launched via `npx` instead, so they need
  [Node.js](https://nodejs.org/) installed (which also gives you `npx`) --
  no extra pip package for either.

**Optional: the Windows desktop automation arm.** Off by default
(`ENABLE_WINDOWS_AUTOMATION=false`); Windows-only. `pip install pywinauto pillow`
to turn it on (Pillow is only for screenshots) -- see **Windows desktop automation arm** above.

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

When the task finishes (or fails), the final answer/error is printed and a
structured result record is saved to `output/<timestamp>.json` -- see
**Architecture** above for its shape. A full log of the run is written to
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

MCP arms (only work once the corresponding `ENABLE_MCP_*` flag is on --
see **MCP arm** above):

9. (fetch) `Fetch https://en.wikipedia.org/wiki/Playwright_(software) and summarize what it's for.`
10. (Brave Search) `Search the web for the current version of Playwright and tell me what it is.`
11. (filesystem) `List the files in the folder you have access to, then read the first one and summarize it.` (points at whatever `MCP_FILESYSTEM_ROOT` is set to)

Windows arm (only works once `ENABLE_WINDOWS_AUTOMATION=true` -- see
**Windows desktop automation arm** above):

12. `Launch Notepad, type "Hello from the agent" into it, and tell me what the window's title bar says.`

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
6. **Milestone 6** (MCP arms, one at a time): turn on one `ENABLE_MCP_*`
   flag, try its matching task (#9, #10, or #11 above), confirm it works,
   then move to the next -- the same "prove the mechanism before trusting
   it" approach used for the browser/Excel arms.
7. **Milestone 7** (Windows arm): set `ENABLE_WINDOWS_AUTOMATION=true`,
   `pip install pywinauto`, try task #12 above -- confirms the arm can
   launch a real app, type into it, and read back its own result.

## Voice interface

Control the agent by talking to it (Windows):

```
pip install faster-whisper sounddevice pyttsx3
python voice.py
```

- **Hold right Ctrl** (`VOICE_PTT_KEY`) while you say a task, e.g. "Open
  Notepad and type hello world", and release it. The agent runs the task
  exactly like `python agent.py` would, then **says the result** aloud.
- **Press F10** (`VOICE_STOP_KEY`) to stop a running task before its next
  action. Ctrl+C in the window quits.
- **Confirmations are spoken.** Before a risky action the agent asks aloud
  and listens for ~4 seconds. **Only a plain "yes" or "confirm"
  continues**; silence, "no", "yes please", or anything it can't make out
  declines, the same as typing `n`.
- **Privacy:** the microphone records only while the key is held (and for
  the few seconds after a confirmation question). Speech-to-text runs on
  your PC (faster-whisper); audio is never uploaded or saved. Only the
  transcribed sentence becomes the task text.
- The first run downloads the speech model (`VOICE_WHISPER_MODEL`, default
  `base.en`, ~150 MB) once. `small.en` is more accurate but slower; `tiny.en`
  is fastest.
- **Quick commands run instantly** (well under a second, no full agent run):
  "open notepad" / "open calculator" (apps on `SAFE_APPS`), "go to youtube",
  "open example dot com", "search youtube for lofi beats", "volume up",
  "mute", "pause", "next track", "take a screenshot" (saved to
  `output/screenshots/`). With `TYPESAFE_API_KEY` set, Jev also
  catches other phrasings ("fire up the calculator"), only when it's at
  least `QUICK_MIN_CONFIDENCE` sure. Anything longer ("open notepad and
  type hello"), unsure, or not on the lists runs through the full agent as
  before. Turn off with `VOICE_QUICK_COMMANDS=false`.
- For faster steps, combine with `DECIDER=hybrid` (see **Configuration**).
- **Troubleshooting:** `python voice.py --keys` prints the name of each key
  you press, as the program reads it; put the one you want in
  `VOICE_PTT_KEY`. Keys are read by asking Windows whether they're held
  down (no keyboard hook), so it works without admin rights; if the window
  in front runs as administrator and Python doesn't, Windows may hide its
  key presses. `python voice.py
  --mic-test` records 4 seconds with no key needed and shows what was heard,
  which checks the microphone and speech model on their own. Windows must
  allow microphone access for desktop apps (Settings > Privacy & security >
  Microphone).

## Discord bot interface

`discord_bot.py` lets you DM the agent a task from your phone (or any
device with Discord) from anywhere -- not just the same Wi-Fi -- since the
bot makes an outbound connection to Discord; nothing on this PC listens for
inbound connections, so there's no port forwarding, firewall rule, or VPN
to set up.

**One-time setup** (you do this part, not the agent):

1. Go to <https://discord.com/developers/applications> → **New
   Application** → give it a name.
2. **Bot** tab → Reset/copy the **bot token** → put it in `.env` as
   `DISCORD_BOT_TOKEN`. Never commit this.
3. Same **Bot** tab → enable the **"Message Content Intent"** toggle. Easy
   to miss, and the bot silently can't read any message text without it.
4. Either invite the bot to a server you're in (**OAuth2** tab → URL
   Generator → scope `bot` → permissions `Send Messages` +
   `Read Message History` → open the generated URL), or skip that and just
   DM the bot directly once you know its user ID -- simpler and more
   private, no server needed.
5. Get your own Discord user ID (Discord app → **Settings** → **Advanced**
   → enable **Developer Mode**, then right-click your own name → **Copy
   User ID**) → put it in `.env` as `DISCORD_ALLOWED_USER_ID`. The bot only
   ever acts on messages from this exact user; everyone else is silently
   ignored.

**Running:**

```
python discord_bot.py
```

This is a separate long-running process from `python agent.py`, which is
unchanged and still the terminal entry point.

DM the bot a task the same way you'd type it at the `python agent.py`
prompt. It replies with an immediate `🤖 Running: <task>` acknowledgment,
then the final result (or a `WHAT HAPPENED` / `WHY` / `WHAT YOU CAN DO`
failure explanation) once it's done -- these can take 30-90+ seconds since
it's driving real Chrome/Excel automation underneath.

If a task hits a sensitive-action confirmation, the bot asks right in the
chat:

```
Ready to click <button 'Submit'>. This looks like it may have side effects. Continue?
Reply y to continue or n to decline (auto-declines in 5 minutes).
```

Reply `y` or `n`. Anything else, or no reply within 5 minutes, is treated
as a decline -- the same fail-closed default as an explicit `n`, never a
silent auto-approve.

Bot-triggered tasks always run with Chrome headless (nobody's watching this
PC's screen remotely), and only one task runs at a time -- send a second
one while the first is still going and the bot replies "Still working on
the previous task" instead of racing two Chrome/Excel sessions against
each other.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `anthropic`, `openai`, `deepseek`, `gemini`, `openrouter`, or `mock` (mock is for tests only) |
| `LLM_MODEL` | Model name for that provider, e.g. `claude-sonnet-5` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Your API key, never hard-coded |
| `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | Key for `LLM_PROVIDER=deepseek` / `gemini` / `openrouter` (cheaper models; needs `pip install openai`) |
| `OPENAI_BASE_URL` | Optional: another OpenAI-compatible address, e.g. a local LM Studio server |
| `DECIDER` | `claude` (default): the LLM above decides every step. `hybrid`: TypeSafe's Jev decides click/type/scroll steps on web pages and click/type/re-list steps in a listed Windows app window (much faster), the LLM above everything else -- see `jev.py` |
| `TYPESAFE_API_KEY` | Required if `DECIDER=hybrid` -- from https://console.typesafe.ai |
| `TYPESAFE_MODEL` | Jev model name; default `jev-latest` |
| `JEV_MIN_CONFIDENCE_WINDOWS` | Same, inside Windows app windows (default `0.8`), where Jev can't see each click's effect |
| `JEV_MIN_CONFIDENCE` | Below this (default `0.5`) a Jev pick is ignored and the LLM above decides the step |
| `MAX_STEPS` | Hard cap on observe/decide/act/verify cycles per task (cost control) |
| `STEP_TIMEOUT_MS` | Playwright timeout per page load/action |
| `MAX_DOM_CHARS` | How much page text is sent to the LLM per step (cost control) |
| `HEADLESS` | `false` shows the Chrome window (recommended while learning) |
| `CHROME_CHANNEL` | `chrome` = drive your real installed Chrome |
| `CHROME_EXECUTABLE_PATH` | Override if Chrome is in a non-standard location |
| `CHROME_USER_DATA_DIR` | Persistent profile folder, so manual logins carry over between runs |
| `USE_PERSISTENT_PROFILE` | `true` keeps cookies/logins between runs |
| `CONFIRM_SENSITIVE_ACTIONS` | `true` asks `[y/n]` before risky (tier R2) actions |
| `CONFIRM_R1_ACTIONS` | `true` also asks before reversible, in-memory-only writes (e.g. `excel_write_cell`); `false` by default |
| `ENABLE_MCP_FETCH` | `true` adds the read-only MCP "fetch" arm (`mcp_fetch`); `false` by default -- see **MCP arm** above |
| `MCP_FETCH_COMMAND` | Command used to launch the fetch MCP server; default `mcp-server-fetch` (must be on PATH) |
| `ENABLE_MCP_BRAVE_SEARCH` | `true` adds the read-only Brave Search arm (`mcp_brave_*`); `false` by default -- see **MCP arm** above |
| `BRAVE_API_KEY` | Required if `ENABLE_MCP_BRAVE_SEARCH=true` -- free key from https://brave.com/search/api/ |
| `ENABLE_MCP_FILESYSTEM` | `true` adds the read-only filesystem arm (`mcp_read_text_file`, etc.); `false` by default -- see **MCP arm** above |
| `MCP_FILESYSTEM_ROOT` | Required if `ENABLE_MCP_FILESYSTEM=true` -- the one local folder the agent may read from |
| `MCP_STARTUP_TIMEOUT_S` | How long to wait for an MCP server to start before giving up; default `90` (an npx-launched server can be slow on a cold npm registry round-trip) |
| `VOICE_PTT_KEY` | Voice: hold this key to talk (default `ctrl_r` = right Ctrl; also `f9`, `alt_gr`, `scroll_lock`, a letter... -- `python voice.py --keys` shows names) |
| `VOICE_STOP_KEY` | Voice: stops a running task before its next action (default `f10`) |
| `VOICE_WHISPER_MODEL` | Voice: local speech-to-text model, `tiny.en` / `base.en` (default) / `small.en` |
| `VOICE_LANGUAGE` | Voice: spoken language code, default `en` (use a multilingual model like `base` for others) |
| `VOICE_QUICK_COMMANDS` | Voice: run simple one-step commands (open app/site, search, volume, media keys) instantly; default `true` |
| `QUICK_MIN_CONFIDENCE` | Voice: how sure Jev must be (default `0.8`) to treat another phrasing as a quick command |
| `DISCORD_BOT_TOKEN` | Bot token for `discord_bot.py`; it refuses to start without one |
| `DISCORD_ALLOWED_USER_ID` | Your Discord user ID; `discord_bot.py` ignores everyone else |
| `SAFE_APPS` | Apps that open without a `[y/n]` when launched by bare name with no arguments; default `notepad.exe,calc.exe,mspaint.exe,snippingtool.exe,explorer.exe`, empty = always ask |
| `ENABLE_WINDOWS_AUTOMATION` | `true` adds the Windows desktop automation arm (`windows_*`); `false` by default, Windows-only -- see **Windows desktop automation arm** above |

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
  Declining stops the task immediately with an explanation. One exception:
  a plain search (typing into a search box of a form that submits with GET,
  like Google's, YouTube's or Wikipedia's) doesn't ask -- its result is just
  a URL the agent could open anyway. It asks again if `CONFIRM_R1_ACTIONS=true`.
  By voice, silence gets one "I didn't hear an answer" retry; only a plain
  "yes" continues.
- `excel_save` gets the same treatment -- it's the only Excel action that
  touches disk (reads and `excel_write_cell` only change the in-memory
  workbook), so it always asks before overwriting a real file:
  ```
  Ready to save the workbook to 'C:\...\report.xlsx', overwriting it. Continue? [y/n]
  ```
- The contents of password fields (and fields labelled PIN, CVV, card
  number, token, API key, one-time code, ...) are never sent to the LLM: the
  model sees the field's label, and `[hidden]` instead of its value. On
  Windows, UI Automation's own `IsPassword` flag masks a control in
  `windows_list_controls` and blocks `windows_read_control_text` on it. See
  `secret_fields.py`.
- With `DECIDER=hybrid`, browser and Windows-window steps are also sent to
  TypeSafe (task, page URL/title/visible text or the window's control
  list, element labels -- the same data the LLM sees, secret fields and
  password boxes already masked). Jev only *chooses* among the page's
  own elements; every choice goes through the same risk tiers and `[y/n]`
  confirmations as the LLM's, and anything Jev is unsure of, or can't
  express (a URL, Excel values, a login page, the final answer), is decided
  by the LLM instead.
- No password, API key, cookie, or session token is ever written to a log
  file (`logger.py` also redacts anything that looks like a secret as a
  defense in depth).
- The Discord bot only ever obeys `DISCORD_ALLOWED_USER_ID`; every other
  message is silently ignored. Sensitive-action confirmations still fire
  over Discord chat (see **Discord bot interface** above) -- a `[y/n]`
  question is never skipped just because the request came in remotely.

## Cost awareness

**Default since 2026-09-25: `deepseek` / `deepseek-flash`** (see
ARCHITECTURE_DECISIONS.md §2a). For tasks involving private information,
switch to `anthropic` / `claude-sonnet-5`.

**Choosing a cheaper model.** Measured on this agent (2026-09-24 evals): a
Claude step sends ~6,600 tokens, ~84% of them cached, and gets ~100 back.
At September 2026 list prices that's roughly, per step:

| `LLM_PROVIDER` / `LLM_MODEL` | Per step | vs. Sonnet 5 |
|---|---|---|
| `anthropic` / `claude-sonnet-5` (default) | ~$0.0042 | 100% |
| `anthropic` / `claude-haiku-4-5` | ~$0.0021 | 50% (but failed 2/9 evals and took more steps) |
| `openai` / `gpt-5-mini` | ~$0.0006 + its thinking tokens | ~15-30% |
| `gemini` / `gemini-3.1-flash-lite-preview` | ~$0.0006 | ~13% |
| `deepseek` / `deepseek-flash` | ~$0.0005 (half off-peak) | ~6-11%; **measured 2026-09-25: 11/11 evals, ~90% cheaper and 20% faster than Sonnet** |

Cheaper models tend to take more steps or fail more, so compare them on the
eval suite before switching: `python evals/run_evals.py --save
evals/results/<model>.json` and keep the one that passes everything at the
lowest real cost. Privacy: whichever provider you pick receives the task and
the page/window text on every step it decides (secret fields are masked);
DeepSeek's servers are in China. `DECIDER=hybrid` already moves click/type
steps to Jev (~$0.0002 per step), so the cheaper model only replaces the
steps the LLM still decides.

- `MAX_STEPS` hard-caps how many LLM calls a single task can make.
- Only a compact, text-only observation (not a screenshot, not full HTML)
  is sent per step, capped at `MAX_DOM_CHARS` characters.
- A short action history (last 8 steps) is included so the model has
  context without resending the whole conversation.
- If the model repeats the exact same action 3 times in a row, the agent
  assumes it's stuck and stops rather than burning further API calls.
- With `LLM_PROVIDER=anthropic`, the system prompt and tool definitions
  (about 2.5-4k tokens, the same on every step) are prompt-cached: from the
  second step of a task on, that part is billed at about a tenth of the
  normal input price. Caching needs at least 1,024 prompt tokens on
  `claude-sonnet-5` but 4,096 on `claude-haiku-4-5`, so on Haiku it usually
  doesn't kick in. `output/*.json`'s `token_usage` shows it as
  `cache_read_input_tokens` / `cache_creation_input_tokens`.

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
  into in normal Chrome** -- the agent uses its *own* persistent profile
  (`CHROME_USER_DATA_DIR`), separate from your everyday Chrome profile, so
  it starts logged out everywhere. Run the agent once, let it open the
  site, and log in manually in that window -- it'll be remembered next
  time.
- **"The agent repeated the same action three times without progress"**,
  **"...is oscillating between two actions..."**, or **"...made no
  observable progress for N consecutive actions"** -- the model got stuck
  (three different ways of detecting the same underlying problem); try
  rephrasing the task more specifically.
- **Task fails with "did not finish within MAX_STEPS"** -- either raise
  `MAX_STEPS` in `.env`, or split the task into smaller ones.
- **Model reply is missing valid JSON** -- rare, but can happen with very
  small/local models; try a more capable model in `LLM_MODEL`.

## Running the tests

The test suite runs entirely offline against local fixture pages
(`tests/fixtures/`) using a scripted `mock` LLM provider, so it needs no
API key and no internet access. This includes `discord_bot.py`'s own
plumbing (permission gating, the pending-confirmation state machine, the
single-task lock) with no real Discord connection made -- see
`tests/test_discord_bot.py`:

```
pip install pytest
pytest tests/ -v
```

The same command also runs automatically via GitHub Actions
(`.github/workflows/tests.yml`) on every push/PR to `main`.

This proves the *mechanism* is correct -- it never proves a real model
completes real tasks well, since nothing here calls a real LLM. For that,
see `evals/README.md`: a small suite of representative tasks run against
your own real, configured LLM (`python evals/run_evals.py`), scored by
checking what the agent actually did rather than trusting its summary.

## What this deliberately does *not* do

No web UI, no mobile app (Discord is the remote interface instead), no
database, no multi-agent system, no always-on background process, no
long-term memory across tasks, no Docker, and never a CAPTCHA/MFA bypass
(permanent, not a scope-for-now cut). See `ARCHITECTURE_DECISIONS.md`'s
"Explicitly deferred" table for the other candidates considered and set
aside, and why.
