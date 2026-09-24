# Improvement plan: TypeSafe Jev decisions + voice control

**Status:** proposal, not yet agreed. Written 2026-09-24 after studying Rocky and the three projects it is built from. Nothing here is implemented yet.

**Goal (from the user):** control the machine by voice, and make this agent fast the way Rocky is, by using TypeSafe's Jev model. Keep what this project already does well and adopt what those projects do well.

**Recommendation in one line:** add Jev as a second *decider* next to Claude, not a replacement. Jev picks the next action on screens where the choice is a pick from a list (browser, Windows controls, spoken commands). Claude handles the parts Jev cannot do: writing text, planning, breaking ties, checking that a task is really done. Voice becomes a new front-end that calls `run_task()`, the same way `discord_bot.py` does. The risk tiers, confirmations, login-wall stops and offline tests stay exactly as they are.

---

## 1. What was studied

| Project | What it is | What matters for us |
|---|---|---|
| [BenjisCollector/rocky](https://github.com/BenjisCollector/rocky) (~4.8k lines, v0.1.0, 2026-09-21) | Voice agent for macOS: wake word, fast path for simple commands, accessibility-tree goal loop | Two tiers (fast command / screen goal), select text instead of generating it, risk rules decided in code, run folders, its own review doc (`docs/REVIEW-2026-09-20.md`). Windows is **untested**; Windows speech-to-text isn't implemented. |
| [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (~850 lines) | Browser agent where Jev picks operation + element in one request | The core pattern for our browser arm. Measured: Google Flights in 7.1 s, 17 Jev requests, **median Jev latency 178 ms**. One-call DOM snapshot cut browser protocol calls **1,092 → 101** and task time 25%. |
| [kevinbadi/jev-voice](https://github.com/kevinbadi/jev-voice) (~6.8k lines) | Voice control for Mac plus a hybrid "Agent" mode | **The closest match to what we want:** Jev decides first, and a Claude model is called only when Jev is unsure (`escalate.py`: plan, text value, tie-break, verify-done). Also cost accounting (`costs.py`). |
| [awlevin/typesafe-computer-use](https://github.com/awlevin/typesafe-computer-use) (~5.8k lines) | Computer-use loop around Jev, macOS plus experimental Windows | Measured Jev vs Claude Opus 5 on the same screen: **$0.0002 vs $0.032 per decision, 0.13–0.38 s vs 5.2 s**. Its honest caveat: Jev can't reason from pixels, so every piece of reasoning a frontier model does for free has to be rebuilt as deterministic state. Emergency stop: slam the mouse into a screen corner. |

**About Jev itself** (from the three repos' code and published pricing; docs.typesafe.ai was blocked from this session, so **verify these before building**):
- One HTTP call, `POST https://api.typesafe.ai/v1/systemone`, with `{model, state, questions}`. It returns one answer per question.
- Question types: `choice` (pick one id from a list, up to about 255 options, with a probability for each and a confidence), `noul` (yes/no probability) and `score`. Several questions go in one request and are answered in parallel. This is the "fan-out" trick: operation plus target in one round trip.
- **It never generates text.** It can only choose from ids we offer. That is why it is fast and why its output is easy to validate, and it is also its main limit.
- Price about **$0.042 per million input tokens, output free**. A ~5k-token decision costs about $0.0002. Reported latency 70–500 ms. Context is about 64k tokens for state plus questions.
- Data: TypeSafe says API data isn't used for training. A zero-data-retention route is reported via Vercel's AI Gateway.

---

## 2. Does this pass our own §6 test? (`ARCHITECTURE_DECISIONS.md`)

1. **A capability we demonstrably lack?** Yes: per-step latency and cost. Every step today is a full Claude tool-calling round trip, measured at seconds per step in comparable setups. Voice control needs sub-second reactions to feel usable. We should still **measure our own baseline first** (Phase 0), not assume it.
2. **Validated work discarded?** None, if done additively. `browser.py` observation/actions, the login-wall logic, `tool_provider.py` risk tiers, the VERIFY step, stuck-loop guards, the Windows arm's hard-won fixes (bugs 7–11) and the Excel/MCP arms all stay. Jev only replaces *which tool to call next* on screens where that is a pick from a list.
3. **Scope match?** Yes. Single user, single machine, personal tool, same as Rocky and jev-voice.
4. **Additive?** Yes. A new `JevDecider` sits behind `LLMClient.decide_next_action()`'s existing return shape (`{"action", "thought", "args"}`), selected by a `DECIDER` setting. `agent.py` still dispatches by tool name and never knows which decider answered.

This is not a runtime migration. §3 stays intact, and Claude stays the model this project is built on.

---

## 3. Keep, adopt, don't adopt

### Keep (our strengths; the reference projects are weaker here)
- **Risk tiers R0–R3 with fail-closed R3** (`tool_provider.py`). Rocky has a keyword list; ours is a real per-tool contract, and MCP tools default to R3.
- **Login/CAPTCHA/MFA walls stop and hand back control** (`browser.py` `LOGIN_WALL_PHRASES` plus the model's `login_required`). None of the Jev projects have this.
- **The VERIFY step and three guards** (exact repeat, A-B-A-B oscillation, consecutive no-effect).
- **Multiple arms** (browser + Excel + MCP + Windows) in one task. The Jev projects each do one surface.
- **Pluggable `confirm_callback`**: voice confirmation plugs in here with no change to the loop.
- **Offline tests plus separate real evals**, and secret redaction in `logger.py`.
- **The Windows arm's lessons**: UIA `invoke()` over coordinate clicks, paced `type_keys()`, exact-title window matching.

### Adopt
| From | Idea | Where it lands |
|---|---|---|
| jev-ultrafast | **Operation + target in one Jev request.** `operation` choice, plus one target head per operation containing only compatible elements (e.g. `TYPE_TEXT` offers only editable fields). Read only the head for the chosen operation. | new `jev.py` + decider in `llm.py` |
| jev-ultrafast, rocky | **`validate_choice`**: reject any answer outside the offered ids, with non-finite probabilities, or where the probabilities don't sum to about 1. Nothing executes on a bad answer. | `jev.py` |
| jev-ultrafast | **One-call page snapshot**: read all interactive elements, labels, values and states in a single `page.evaluate()` instead of 5–6 calls per element. Worth doing even with Claude. | `browser.py` `observe()` |
| jev-ultrafast | **Freshness guard before input**: re-check that the target still has the same role/label/value, is visible and enabled, and isn't covered, before clicking or typing. Stale → re-observe, don't act. | `browser.py` `click()`/`type_text()` |
| jev-ultrafast | **Short settle waits**: ≤2 animation frames or 50 ms after a click; up to 200 ms for autocomplete suggestions after typing into a combobox. | `browser.py` |
| jev-voice | **Escalate to Claude only when needed**: Jev `BLOCKED`, low confidence (below `JEV_MIN_CONFIDENCE`), or top two options close → Claude picks from Jev's candidates. Claude Haiku first, the configured `LLM_MODEL` if Haiku is unsure. | decider in `llm.py` |
| jev-voice, typesafe-computer-use | **Claude checks `DONE`**: Jev choosing DONE is a claim, not proof. Claude confirms against page text before we accept `finish` and writes the summary. This keeps our "summary must have real content" rule. | decider |
| all three | **Text is written, not chosen**: when the operation is `TYPE_TEXT`, first try a span cut from the user's own words (quoted text, "type …", "search for …"). If none fits, Claude writes it as a strict `{"text": …}` under 200 characters, rejecting any control character (Rocky's H1 bug: a `\n` became an Enter that sent a message). | new `writer.py` |
| rocky | **Two-tier voice routing**: simple commands (open app, go to site, search, shortcut, volume, media) take a *fast path* of one Jev request plus one action. Anything else becomes a goal passed to `run_task()`. | new `voice_router.py` |
| rocky | **Focus-moved refusal**: record the foreground window when the command is heard. If it changed before we send keystrokes, refuse ("Focus moved to Teams. Nothing sent."). | `windows_tools.py` + fast path |
| rocky | **Never type into secret fields**: refuse to type into password/PIN/card fields (`IsPassword` in UIA, `type=password` in DOM) even after a yes. | browser + Windows arms |
| typesafe-computer-use | **Emergency stop**: a global hotkey (and optionally the mouse slammed into a screen corner), checked before every action. | voice front-end + `agent.py` |
| jev-voice | **Cost accounting**: Jev tokens × price next to Claude tokens in the output JSON. | `llm.py` `get_usage()` / `_save_output` |

### Don't adopt
- **Rocky's keyword risk classification instead of our tiers.** Our tiers are stricter. We could add its extra words (e.g. "publish", "post", "approve", "sign", "wipe") to `SENSITIVE_KEYWORDS`.
- **DeepSeek as default writer/planner** (Rocky, jev-ultrafast). We already have Claude keys, and sending utterances to a third provider adds a privacy surface for no gain.
- **Coordinate clicks from accessibility frames** (Rocky's macOS path). Our Windows arm already found that `invoke()` beats synthetic mouse input (bug 9).
- **Logging everything heard** (Rocky's `history.jsonl`). Log commands that were acted on, never raw audio or ignored chatter.
- **A persistent Caps Lock remap LaunchAgent** (jev-voice). Too invasive; use a normal hotkey.
- **Chess/recommend/persona features.** Out of scope.

---

## 4. Target shape

```text
             voice (mic → VAD → local Whisper)          CLI / Discord (unchanged)
                          │                                     │
                 voice_router.py: ONE Jev request               │
          kind · app · site · engine · shortcut · text-span      │
             │                         │                        │
       FAST path                   GOAL path ─────────────► run_task(task, confirm_callback=voice_confirm)
   (one ToolProvider call,              │
    same risk gate)                     ▼
                              agent.py loop (unchanged dispatch, risk tiers, VERIFY, guards)
                                        │
                        LLMClient.decide_next_action()  ← DECIDER=claude | jev | hybrid
                          │                 │
                   JevDecider          Claude (today's path)
                  op + target heads     escalation: tie-break, TYPE_TEXT writer,
                  (browser, Windows)    DONE check, Excel/MCP free-form args
```

Rule for which decider answers a step in `hybrid` mode:
- A browser or Windows screen where the next step is click / type-target / select / scroll / wait / done / blocked → **Jev**.
- The step needs free-form arguments (a URL to `goto`, Excel cell values, MCP tool args, a file path), or Jev is unsure / blocked / claims done → **Claude**.

---

## 5. Phases

Each phase ends with `pytest tests/ -q` green and, where marked, a real-run check. Real end-to-end runs found most of our bugs (ARCHITECTURE_DECISIONS §1), so a green test run alone isn't "done."

### Phase 0: measure first, plus one free speed-up (no Jev needed)
- Add per-step timing (observe / decide / act, in ms) and per-provider token usage to `output/*.json` and to `evals/run_evals.py`'s report.
- Run the existing eval suite with Claude and record the **baseline**: median seconds per step, steps per task, tokens and $ per task.
- Rewrite `browser.py` `observe()` to read all elements in **one** `page.evaluate()` call, keeping the same `Observation` shape and `ElementInfo` fields so every existing test passes unchanged. This is jev-ultrafast's biggest measured win, and it speeds up the Claude path too.
- **Exit:** a baseline table in `evals/README.md`; the snapshot rewrite passes all browser tests plus the local fixture evals.

**Status 2026-09-24:**
- Done: per-step timing in every output record; `run_evals.py --save`.
- Done: the one-call snapshot. `evals/bench_observe.py` measured 41–59× faster page reads (200 elements: 4.35 s → 74 ms; 500 elements: 10.8 s → 0.21 s), and the old and new output was checked identical on every fixture. This mattered more than expected: before it, reading a typical 200–500 element page cost **4–11 s per step** in this container, likely more than the Claude call itself. Jev's speed advantage should be re-judged against the *new* numbers, not the old ones.
- Done (user's PC, claude-sonnet-5): 9/9 evals passed. Median step: **Claude decision 2,275 ms**, page read 53 ms, action 21 ms. Decisions were 78% of total task time. Prompt caching served 84% of prompt tokens; the whole run cost about $0.15 (about $0.50 uncached). Table in `evals/README.md`, raw file in `evals/results/`.
- **What this changes for Phase 1:** cost is no longer an argument for Jev (about $0.004 per Claude decision already). Speed is: Jev's reported 0.13–0.38 s per decision against Claude's measured 2.3 s would cut roughly 2 s from every browser/Windows step, which is what voice control needs. The Phase 5 comparison should be run against this file.

### Phase 1: Jev client and browser decider (behind a flag, default off)
- `jev.py`: one `httpx` client, retry once on timeout/5xx, `JevError` → the same `LLMError` path, `validate_choice()`. Nothing executes on an invalid answer.
- `config.py`: `TYPESAFE_API_KEY`, `TYPESAFE_MODEL=jev-latest`, `DECIDER=claude|jev|hybrid` (default `claude`), `JEV_MIN_CONFIDENCE`. Add a `TYPESAFE_API_KEY` pattern to `logger.py` redaction (defense in depth, not the only guard).
- `llm.py`: `JevDecider` builds the element table from `Observation.elements`, asks `operation` + `click_target` / `type_target` / `select_target` in one request, and returns the normal `{"action", "thought", "args"}` dict. `thought` holds the confidences, so logs stay readable.
- Mapping: `BLOCKED` → escalate to Claude, never skip a wall. Our login-wall heuristic still runs first, and `login_required` stays a Claude/heuristic decision. If a page has more than about 250 elements, rank and truncate them; truncated elements can't be chosen.
- `tests/`: a `MockJev` with scripted answers (fully offline), covering: invalid choice rejected, wrong head ignored, low confidence escalates, a `DONE` claim escalates to the check, and a risk-tier confirmation still fires on a Jev-chosen submit click.
- **Exit:** the fixture evals pass with `DECIDER=hybrid`, and the Phase 0 table gets a hybrid column.

### Phase 2: writer, DONE check, safety adoptions
- `writer.py`: span-first, then Claude (Haiku by default) with a strict JSON reply: one `text` key, 1–200 characters, no control characters. Never fills fields labelled password/PIN/card/CVV/SSN/token.
- Claude `DONE` check writes the `finish` summary, keeping bug 2's guarantee (a summary with real content).
- *(Done ahead of this plan, 2026-09-24: secret field values are masked before reaching any model, in both arms. See `secret_fields.py` and ARCHITECTURE_DECISIONS §1 bug 12. A Jev decider must build its element table from the same masked `ElementInfo`.)*
- Freshness guard before browser click/type; refuse to type into `type=password` inputs and UIA `IsPassword` controls; add Rocky's extra destructive words to `SENSITIVE_KEYWORDS`.
- **Exit:** tests for each refusal; a real run of the multi-field form fixture with the confirmation still firing.

### Phase 3: voice front-end on Windows
- `voice.py`, all local:
  - microphone via `sounddevice`
  - energy VAD (Rocky's adaptive noise floor)
  - speech-to-text with **faster-whisper** (runs well on Windows CPU/GPU; nothing leaves the machine)
  - **push-to-talk hotkey first**; a wake word ("hey agent", fuzzy-matched like Rocky's `strip_wake`) second.
- `voice_router.py`: one Jev request per utterance with Rocky's question set, adapted to our arms:
  - `open_app` → `windows_launch_app`
  - `open_url` / `search` → browser `goto`
  - `shortcut`, `volume`, `media` → a small new Windows fast-path provider, each tool risk-classified explicitly (new tools default to R3 per the invariant)
  - `goal` → `run_task()`
  - `none` / not addressed → ignore
- `voice_confirm` callback: speaks the question (Windows SAPI via `pyttsx3`) and listens about 5 s. **Only a plain "yes" or "confirm" continues; silence or anything else declines.** Stop capture during the confirmation window so the "yes" isn't queued as a new command (Rocky's L1 bug).
- Emergency stop hotkey checked before every dispatch.
- **Exit:** a 10-command spoken session on the real machine (open app, go to site, search, type, scroll, volume, one browser goal, one Windows goal, one declined confirmation, one emergency stop), with logs as evidence.

### Phase 4: Windows arm on Jev
- Build the element table from `windows_list_controls` output and use the same operation + target heads as the browser. Keep `invoke()`, paced `type_keys()`, exact-title matching and dynamic click risk unchanged.
- **Exit:** the Notepad and Calculator end-to-end tasks from bugs 9–11 pass with `DECIDER=hybrid`.

### Phase 5: decide the default from numbers
- Run evals for `claude` and `hybrid`: success rate, median time, $ per task, number of escalations.
- Make `hybrid` the default **only if** its success rate is no worse and it is clearly faster or cheaper. Record the decision and numbers in `ARCHITECTURE_DECISIONS.md`.

---

## 6. How the non-negotiable invariants are kept (CLAUDE.md)

| Invariant | How this plan keeps it |
|---|---|
| Never bypass login/CAPTCHA/MFA | Heuristic runs before Jev; Jev `BLOCKED` escalates, it never "tries harder"; `login_required` unchanged |
| R1/R2/R3 tiers, R3 always confirms | Every Jev-chosen action goes through the same `_dispatch_action()` → `requires_confirmation()`. Jev never assigns risk. Voice fast-path tools are new tools → R3 until classified in code |
| New tools default R3 | Fast-path volume/media/shortcut tools start at R3 and are lowered only explicitly |
| No secrets in logs | Jev key never logged; redaction pattern added as backup; voice never logs raw audio |
| `tests/` offline | `MockJev` + fake microphone/STT; real TypeSafe/Whisper runs go in `evals/` only |

---

## 7. Risks and open questions

- **TypeSafe is new.** Jev launched in September 2026, and these limits and prices come from third-party code and articles because the docs were blocked from this session. Mitigation: `DECIDER=claude` always works; hybrid falls back to Claude on any `JevError`.
- **Privacy.** In hybrid mode, visible page/window text and field values go to TypeSafe on every Jev step. That is the same kind of data Claude already sees today, but it's a second company. Consider the zero-data-retention gateway, and never include secret-field values (Rocky's open M2 finding).
- **Jev can't reason.** typesafe-computer-use found that anything needing comparison (dates, prices, "cheapest") must be computed in code or escalated. Expect Claude escalations on research-style tasks; the speed gain is biggest on navigation and form steps.
- **Voice on Windows is unproven in every reference project.** Rocky and typesafe-computer-use both label their Windows paths untested, which is why Phase 3 requires a real spoken session before it counts as done.
- **Cost of mistakes at speed.** A fast wrong click is still wrong. The freshness guard, confidence floor and risk tiers are what make speed safe, so they land before or with the Jev decider, not after.

- **Newest Claude models reject our forced tool call.** `llm.py` sends `tool_choice: {"type": "any"}` (the fix for bug 2: `finish` always has a summary). `claude-opus-5-5` and `claude-fable-5-1` return a 400 for forced tool choice. The default `claude-sonnet-5` (and Opus 5, Haiku 4.5) is fine. Before anyone sets `LLM_MODEL` to one of those two, switch them to `tool_choice: auto` + `strict: true` tools + the existing "you must call a tool" prompt line, and re-run the evals to confirm bug 2 stays fixed.

## 8. Needed from you before Phase 1

1. A TypeSafe API key (console.typesafe.ai). Phase 0 needs only the existing Claude key.
2. Confirm the machine: Windows 11? GPU or not (this affects the faster-whisper model size).
3. Push-to-talk key preference, and whether you want a wake word at all.
4. Approval of the phase order, or which phase to start with.
