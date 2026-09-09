# Architecture Decisions — Personal AI Agent

**Purpose of this document:** a single, standing reference for what this project is, what's actually built and validated, and what's deliberately deferred and why. It exists because this project went through three rounds of external "should we rebuild this on a different framework" research reports, each reviewed and mostly rejected for concrete, project-specific reasons. Read this before proposing another framework migration or a new round of "let's research this again" — the reasoning below already accounts for that question.

If you're a fresh Claude Code session picking this project up: this file is ground truth. The code and its own comments are also ground truth. A research report recommending a rewrite is not, until it's been weighed against what's actually here.

---

## 1. What this is

A personal, local, command-line (+ Discord) AI agent that takes a plain-English task and completes it by acting through one or more "arms." One user, one machine, no product/multi-tenant ambitions.

**Built and validated with real end-to-end runs, not just tests:**
- **Browser arm** (`browser.py`, via Playwright + real Chrome): navigate, click, type, scroll, read page text, detect login/CAPTCHA walls and stop rather than bypass them. Validated on: Google search, multi-page research summarization, a non-Google site (Wikipedia), a full multi-field form fill-and-submit with the safety confirmation firing correctly.
- **Excel arm** (`excel_tools.py`, via openpyxl): open/read/write/save `.xlsx` files, deliberately not mouse/UI automation on a live Excel window. Validated: Excel-only tasks never launch Chrome (lazy browser startup), mixed browser+Excel tasks work in one run.
- **Orchestrator loop** (`agent.py`): observe → decide → act → verify → repeat, up to `MAX_STEPS`. The "decide" step uses **native LLM tool-calling** (not prompted free-text JSON) against a flat list of tools spanning both arms — the model picks exactly one tool per turn, no separate "pick an arm first" step.
- **Discord bot** (`discord_bot.py`): remote command interface. Works from anywhere with the Discord app (not just home Wi-Fi), no port forwarding or VPN needed (outbound-only connection). Sensitive-action confirmation is now pluggable (`confirm_callback` parameter on `run_task()`, defaulting to the terminal `[y/n]` prompt) so the bot can ask for real interactive y/n confirmation in chat instead of always auto-declining.
- **185 automated tests**, fully offline (mocked LLM, local fixture pages, no real API/network needed), covering the full loop, all three arms (browser, Excel, MCP — the fetch, Brave Search, and filesystem servers), the safety-decline path, the pluggable confirmation mechanism, the Discord bot's own state machine, log secret-redaction, the real Anthropic/OpenAI SDK response-parsing logic, config parsing/validation, the manual login-wall-resolution prompt, the actual prompt text built for the model (history truncation, observation formatting), long-page text pagination via `scroll`, and the three-way stuck-loop/no-progress detection.

**Real bugs found and fixed via live testing** (the reason "just replace this with library X" isn't free — this reliability work would have to be re-earned):
1. Two separate login-wall false-positive bugs (bare-word matching flagged Wikipedia's nav "Log in" link; password-field-presence flagged a public input-type demo page) — fixed by moving to specific wall-phrase matching plus trusting the model's own contextual judgment.
2. Model calling `finish` with an empty summary — fixed by switching to native tool-calling so `summary` is a schema-required field, not a key the model can forget mid-generation.
3. A declined `[y/n]` confirmation being silently swallowed by a generic exception handler instead of stopping the task — a real safety bug, fixed with a dedicated exception path.
4. VERIFY step false-negative on checkbox/dropdown clicks (which change neither URL nor visible text) sending the model into a repeated-clicking spiral — fixed by adding per-element state (`state_fingerprint`) to the comparison.
5. `logger.py`'s secret redaction silently not redacting: the bare-key pattern wrapped its whole match in a capturing group, so the "keep the label, redact the value" substitution reproduced the entire secret verbatim with `[REDACTED]` uselessly appended after it — a log file could look redacted at a glance while the raw key sat right there in full. Its character class also excluded `-`/`_`, so realistic hyphen-heavy Anthropic-style keys barely matched at all. Found the same way as the bugs above: writing the test coverage that should have existed from the start (`tests/test_logger.py`) and asserting against the actual bytes written to disk, not just an in-memory function's return value.
6. `scroll` was a complete no-op for text extraction: `observe()` always called Playwright's `inner_text("body")`, which returns the whole document regardless of scroll position, then truncated to `MAX_DOM_CHARS` — so any page longer than that had its tail permanently unreachable no matter how many times the model scrolled, and it was never even told the text was incomplete. Fixed by giving `BrowserSession` a text-reading offset that `scroll` actually advances/retreats and `observe()` slices by (reset on real navigation), plus telling the model explicitly in the prompt when there's more to read. Building the fix immediately surfaced two more bugs in the fix itself, both caught by the tests written alongside it before they shipped: the offset had no upper clamp (enough "scroll down" calls slid past the end of the text into a permanently empty string), and the reset-on-navigation logic was keyed to `observe()` noticing a URL change between two calls, which silently did nothing if a caller navigated away and back without observing in between.

---

## 2. Core design philosophy

**Buy/reuse commodity infrastructure. Build the differentiator.** This principle is correct and every external research report agreed on it. Where the reports and this project's actual practice diverge is on *what counts as "already built"* for this specific project — see §4.

**Verification is a first-class step, not "tool call succeeded = done."** The agent never accepts "I did it" — it checks the actual resulting state (URL/text/element-state for browser, read-back cell values for Excel) and tells the model immediately if an action didn't visibly work.

**Safety is opt-in-safe by default, not model-judgment-dependent.** Sensitive actions (form submission, disk writes) require explicit `[y/n]` confirmation regardless of what the model thinks is fine. Login/CAPTCHA/MFA walls are never bypassed — the agent stops and either asks a human to resolve it live (headed browser) or fails with a clear explanation.

---

## 3. Decision: keep the current architecture. Do not migrate to a new agent runtime or replace the browser/Excel arms.

Three separate rounds of external research (each proposing, with varying framing, "build on OpenAI Agents SDK / Claude Agent SDK + Browser Use/Skyvern + a new repo structure") were reviewed and rejected for the same core reasons, restated once here so they don't need re-litigating:

1. **This isn't a greenfield decision.** "Don't build the runtime/browser engine yourself" is advice for someone starting at zero. This project already paid the reliability cost — four real, debugged production bugs (§1) — on its own `browser.py`/`agent.py`. Migrating to Browser Use or a new SDK means inheriting *that* project's bugs from zero while the fixes already made here either don't transfer or have to be redone.
2. **Provider consistency.** This project is built on Anthropic/Claude, and specifically fixed a real reliability bug (models forgetting to fill `finish`'s summary) by adopting Claude's native tool-calling. Every report defaulted to OpenAI Agents SDK without addressing this — if a pre-built runtime is ever adopted, Claude Agent SDK is the consistent choice, not OpenAI's.
3. **Scope match.** This is a single-user personal tool. Task manager databases, model routing across cost tiers, cost tracking, concurrency limits, graph/multi-agent orchestration, and durable workflow engines (Temporal/Dapr) are production-platform concerns with no current use case here. Building them now is exactly the "boil the ocean" pattern this project has avoided by shipping small, validated slices (Excel shipped as openpyxl-only before xlwings; Discord shipped before any HTTP/LAN alternative was built out further).
4. **The current loop already provides most of what a runtime migration would buy**: tool-calling, a form of sessions (via history), guardrails (confirmation gates), tracing (TaskLogger). The gap is polish, not missing capability.

**This is not a "never revisit" decision.** Migrate the runtime layer if a *concrete* requirement appears that the current loop genuinely can't do — durable multi-day scheduled workflows, real multi-agent handoffs with isolated workers. Not before.

---

## 4. What's actually next (agreed, not yet built)

In order:

1. **Formalize a `ToolProvider` interface.** A behavior-preserving refactor: each arm (`BrowserToolProvider`, `ExcelToolProvider`) implements a common contract —
   ```python
   ToolSpec(name, description, properties, required, risk_level)  # risk_level defaults to "R3" -- safe by default
   ToolProvider.get_tool_specs() -> list[ToolSpec]
   ToolProvider.execute(name, args) -> str | None
   ToolProvider.get_dynamic_risk(name, args) -> RiskLevel | None   # e.g. browser click's real risk depends on the target element
   ToolProvider.verify(name, args, pre_state, post_state) -> str | None
   ```
   Replaces the current string-prefix dispatch (`if action.startswith("excel_")`) and the two hardcoded `ALWAYS_CONFIRM_*` sets with a registry lookup and a generalized R0–R3 risk-tier policy. **Acceptance criterion: all existing tests pass unchanged, and the CLI experience is identical to a user** — this is a refactor, not a feature.
2. **MCP as a third `ToolProvider`.** Additive, not a replacement for the native arms. One read-only server first (fetch) to validate the mechanism end-to-end, a second (Brave Search — web/local/video/image/news search, via Brave's own actively-maintained package, not the deprecated `@modelcontextprotocol/server-brave-search`) proving the pattern generalizes, including a server that needs a secret (an API key, passed as an env var to just that subprocess, never a CLI arg), and a third (local filesystem read access, scoped to exactly one folder the user names — no default directory, and the server itself rejects paths outside it as a second line of defense). All three done, all off by default. The agent's own policy engine — not the MCP server's self-description — decides risk tier; an MCP server's own new/future tools default to `R3` (confirm) until explicitly classified, so a server update can't silently introduce an unreviewed dangerous tool. The filesystem server is the clearest case for why this matters: it ships write/edit/create-directory/move tools in the very same package as its read tools, and only the read ones are classified R0 -- the rest fall through to R3 by deliberately not being listed, not by any special-casing.
3. **Two cheap, high-value additions identified during report review, worth folding into the above rather than treated as separate phases:**
   - An explicit **instruction/data/evidence trust hierarchy** in the system prompt: webpage/file/email content the agent reads is *data*, never an instruction, and can never expand what the model is permitted to do beyond what the user actually asked. (This closes a real gap — indirect prompt injection via untrusted content the browser arm already reads into context — that earlier research rounds didn't address at all.)
   - **Structured JSON result contracts** (status/summary/artifacts/verification, not a bare success string) as a small evolution of the existing `output/*.json` files.

## 5. Explicitly deferred, and why

| Idea | Why deferred |
|---|---|
| Full agent-runtime migration (OpenAI/Claude Agent SDK) | No concrete capability gap yet; real migration cost; see §3 |
| Browser Use / Skyvern replacing `browser.py` | Would discard validated, debugged reliability work; see §3 |
| Task-manager + persistent DB (QUEUED/RUNNING/WAITING_FOR_APPROVAL states) | Current synchronous `run_task()` already handles this in-process; no scheduled/background tasks exist yet to need it |
| Model routing across cost tiers, cost tracking | Solo user, low volume; no evidence of a cost problem to solve |
| Graph orchestration / multi-agent fan-out / "dynamic workflows" | This project's actual tasks (browse → read Excel → compare → write Excel → report) are genuinely sequential chains with real data dependencies at every step — by the fan-out pattern's *own* stated test ("does the next step read the previous step's output?"), there's no independent work here to parallelize. Revisit only if a future task genuinely has independent sub-work (e.g., downloading several supplier quotes at once). |
| Windows desktop automation (general) | Far more open-ended/brittle than browser (DOM) or Excel (file format); scope down to launch-app + known-dialogs only, whenever built, with default-confirm on every action (not opt-in like the browser arm) |
| FastAPI/LAN HTTP remote interface | Superseded by the Discord bot, which needs no exposed port/VPN and gives real interactive confirmation |
| Full 4-layer adversarial test pyramid, 20-task acceptance gate | Right instinct, wrong size for solo-dev bandwidth; add adversarial/prompt-injection test cases incrementally as real gaps are found, not as an upfront program |

---

## 6. How to evaluate the next external research report

Given the pattern so far (three rounds, each independently proposing a full rebuild), any future "should we adopt X" question should be run through:
1. Does X provide a capability the current architecture demonstrably cannot? (Not "is generally good practice" — a *specific* gap.)
2. What validated, debugged work would be discarded to adopt X?
3. Does X match this project's actual scope (single user, personal tool) or a production/multi-tenant scope?
4. Can X be adopted *additively* (as another `ToolProvider`, another test, another prompt-line) rather than as a replacement?

If the answer to (4) is yes, that's almost always the right call over a migration.
