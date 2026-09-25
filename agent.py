#!/usr/bin/env python3
"""
Command-line entry point for the local AI browser agent (Phase 1).

    python agent.py
    Task: Open Google and search for OpenAI.

This file is intentionally the only place the *loop* lives -- everything
else (config, browser control, LLM calls, logging) is a plain module it
calls into. Read this file top to bottom to understand the whole agent:

    observe the page -> ask the LLM what to do -> do it -> check it worked -> repeat

That loop (an "observe, decide, act, verify" cycle, a variant of the
classic ReAct pattern) is the same idea every browser-automation agent
framework (Browser Use and friends included) is built around. Phase 1
implements it directly with Playwright instead of pulling in a heavier
framework, so every step is visible and easy to modify.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from browser import BrowserSession, BrowserToolProvider
from config import LOGS_DIR, OUTPUT_DIR, load_config
from errors import TaskCannotBeCompleted, explain
from excel_tools import ExcelSession, ExcelToolProvider
from llm import ZERO_USAGE, LLMClient, LLMError
from logger import TaskLogger
from mcp_tools import MCPToolProvider, build_brave_search_provider, build_fetch_provider, build_filesystem_provider
from tool_provider import ToolProvider, ToolSpec, requires_confirmation
from windows_tools import WindowsSession, WindowsToolProvider, resolve_known_folders


def ask_confirmation(prompt: str) -> bool:
    """
    The default confirm_callback for run_task(): a terminal [y/n] prompt.
    Any other front-end (e.g. discord_bot.py) passes its own callable with
    this same str -> bool shape instead -- see run_task()'s confirm_callback
    parameter.
    """
    answer = input(f"{prompt} [y/n]: ").strip().lower()
    return answer in ("y", "yes")


MAX_MANUAL_RESOLUTION_OFFERS = 3

# How many consecutive VERIFY "no observable change" warnings (see the
# VERIFY block in run_task()) before the loop first nudges the model to
# try something different, and before it gives up entirely. This catches
# a failure mode the exact-repeat stuck-guard below can't: the model
# trying a series of DIFFERENT actions (different elements, different
# tools) that each individually accomplish nothing -- not literally
# repeating one action, so it would otherwise burn through MAX_STEPS
# before failing with a vague "did not finish" error instead of a precise
# diagnosis. The hint threshold fires once, giving the model a chance to
# self-correct before the harder abort threshold ends the task.
CONSECUTIVE_NO_EFFECT_HINT_THRESHOLD = 2
CONSECUTIVE_NO_EFFECT_ABORT_THRESHOLD = 4


def offer_manual_resolution(url: str, config, dry_run: bool, logger: TaskLogger, attempt: int,
                            handoff: Callable[[str], bool] | None = None) -> bool:
    """
    Called whenever a login/CAPTCHA/verification wall is detected -- either
    by the heuristic in browser.py, or by the model deciding on its own
    (action == "login_required"). The agent itself never solves it (see
    README section 3), but if there's a visible Chrome window, a human can
    solve it right there instead of restarting the whole task. Returns True
    if the caller should re-observe and continue, False if it should give up.

    `attempt` is a 1-based count of how many times this has already been
    offered for the current task; capped so a misdetected wall (or one the
    user genuinely can't clear) can't prompt forever instead of failing
    with a clear error.
    """
    if config.headless or dry_run:
        return False
    if attempt > MAX_MANUAL_RESOLUTION_OFFERS:
        logger.note(f"Manual-resolution offer limit ({MAX_MANUAL_RESOLUTION_OFFERS}) reached; giving up.")
        return False
    if handoff is not None:
        # A front-end (the voice app) asks its own way: "your turn -- log in,
        # then press Continue". Same rule: the person clears the wall, never
        # the agent.
        if not handoff(f"The page at {url} needs you: log in, solve the CAPTCHA or verify in the Chrome window, "
                       f"then press Continue ({attempt}/{MAX_MANUAL_RESOLUTION_OFFERS})."):
            return False
        logger.note("User resolved the wall manually; continuing.")
        return True
    print(f"\nThe page at {url} looks like it needs manual action (login, CAPTCHA, or verification).")
    answer = input(
        f"Resolve it in the Chrome window, then press Enter to continue ({attempt}/{MAX_MANUAL_RESOLUTION_OFFERS}, "
        "or type 'stop' to give up): "
    ).strip().lower()
    if answer in ("stop", "quit", "exit", "n"):
        return False
    logger.note("User resolved the wall manually; continuing.")
    return True


def run_task(
    task: str, config, dry_run: bool = False, llm_client: LLMClient | None = None,
    confirm_callback: Callable[[str], bool] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_step: Callable[[int, str, str], None] | None = None,
    task_updates: Callable[[], list[str]] | None = None,
    ask_user: Callable[[str], str | None] | None = None,
    handoff: Callable[[str], bool] | None = None,
    browser_session: BrowserSession | None = None,
) -> dict:
    """
    Runs one task end-to-end and returns a result dict. Also writes a log
    file to logs/ and, on success, a result file to output/.

    `llm_client` can be injected directly (used by the test suite to pass a
    MockProvider); normally it's built from `config`.

    `confirm_callback` is how a sensitive-action [y/n] confirmation is
    asked. Defaults to ask_confirmation() (a terminal prompt via input()) so
    `python agent.py` behaves exactly as before. A different front-end
    (discord_bot.py) passes its own str -> bool callable instead -- e.g. one
    that posts the question into a chat and blocks for a reply there,
    rather than blocking on a terminal that isn't attached.

    `should_stop`, if given, is checked before each step and again right
    before an action runs; when it returns True the task stops cleanly with
    a "stopped by you" failure. voice.py wires its stop key to it -- the
    spoken equivalent of Ctrl+C that doesn't kill the whole program.

    `on_step(step, thought, action)`, if given, is told about each decided
    step before it runs -- display only (voice.py's window shows "Step 3:
    ..."); anything it raises is ignored.

    Three more hooks for an interactive front-end (the voice app), all
    optional; without them the task runs exactly as before:
    - `task_updates()` returns messages the person sent mid-task ("no, the
      other tab"). They're appended to the TASK text itself -- the one
      trusted slot -- never to the history, where page/app text lives.
    - `ask_user(question)` registers the ask_user tool (user_tools.py) so the
      model can ask the person something; the answer is added to the TASK
      the same way.
    - `handoff(message)` replaces the terminal prompt at a login/CAPTCHA
      wall: the person clears it, then says continue (True) or stop.

    `browser_session`, if given, is a BrowserSession the caller owns and
    keeps open across tasks (the voice app): this run uses it and leaves
    Chrome open at the end, so a video it started keeps playing. Without it,
    each run starts its own and closes it, as before.
    """
    logger = TaskLogger(LOGS_DIR, task)
    user_confirm = confirm_callback or ask_confirmation
    # Time spent waiting on a human to answer [y/n] is not agent speed, so
    # it's measured separately and subtracted from the step's act time.
    confirm_wait = {"ms": 0.0}

    def confirm(prompt: str) -> bool:
        started = time.perf_counter()
        try:
            return user_confirm(prompt)
        finally:
            confirm_wait["ms"] += (time.perf_counter() - started) * 1000
    session = browser_session or BrowserSession(config)  # Chrome isn't launched until first use -- see BrowserToolProvider.ensure_ready
    if browser_session is not None and browser_session.page is not None and not browser_session.is_alive():
        browser_session.reset()  # closed since the last task
    excel_session = ExcelSession()
    windows_session: WindowsSession | None = None  # set below only if ENABLE_WINDOWS_AUTOMATION
    mcp_providers: list[MCPToolProvider] = []  # only non-empty per ENABLE_MCP_* flags -- closed in the finally below

    history: list[str] = []
    user_updates: list[str] = []  # messages from the person mid-task, see task_updates

    def task_now() -> str:
        if not user_updates:
            return task
        return (task + "\n\nMESSAGES FROM THE USER DURING THIS TASK (same person, same authority as the task "
                "above; if they conflict, the latest one wins):\n" + "\n".join(f"- {u}" for u in user_updates))

    result_summary = None
    error_message = None
    empty_finish_attempts = 0
    wall_offer_attempts = 0
    steps_taken = 0
    consecutive_no_effect_actions = 0
    pending_verify: dict | None = None
    llm = None
    # Structured result contract (see ARCHITECTURE_DECISIONS.md section 4):
    # a record of what the task actually touched, saved to output/ for
    # every run -- not just successful ones -- alongside the bare
    # success/result dict this function returns (which stays exactly as
    # it was, since discord_bot.py and the tests depend on that shape).
    artifacts: list[dict] = []
    verification_warnings: list[str] = []
    # Per-step wall-clock timings (observe / decide / act, in ms) for the
    # structured output record -- the baseline docs/JEV_VOICE_PLAN.md's
    # Phase 0 measures before any speed work. A phase a step never reached
    # (e.g. no browser page open yet, or a rejected finish) is left out.
    step_timings: list[dict] = []

    try:
        # One ToolProvider per arm, registered by tool name -- this is the
        # registry that replaces the old string-prefix dispatch (`if
        # action.startswith("excel_")`). Adding a further arm means adding
        # one more provider here; nothing else in this loop needs to
        # change. See tool_provider.py. This runs inside the try block
        # because MCPToolProvider.get_tool_specs() has to actually start
        # its server to discover its tools (unlike the browser/Excel arms,
        # whose specs are static) -- a startup failure there is a
        # TaskCannotBeCompleted like any other hard stop, not a crash.
        providers: list[ToolProvider] = [BrowserToolProvider(session), ExcelToolProvider(excel_session)]
        if config.enable_mcp_fetch:
            mcp_providers.append(build_fetch_provider(config))
        if config.enable_mcp_brave_search:
            mcp_providers.append(build_brave_search_provider(config))
        if config.enable_mcp_filesystem:
            mcp_providers.append(build_filesystem_provider(config))
        providers.extend(mcp_providers)
        if config.enable_windows_automation:
            # No subprocess/thread of its own (unlike the MCP arms above),
            # so no close()/cleanup path is needed in the finally below --
            # see WindowsSession's docstring.
            windows_session = WindowsSession()
            safe_apps = frozenset(a for a in config.safe_apps.split(",") if a.strip())
            providers.append(WindowsToolProvider(windows_session, safe_apps=safe_apps))
        if ask_user is not None:
            from user_tools import UserToolProvider

            def remember_answer(question: str, answer: str) -> None:
                user_updates.append(f"(answering your question \"{question}\") {answer}")
                logger.note(f"User answered {question!r}: {answer!r}")

            providers.append(UserToolProvider(ask_user, remember_answer))

        tool_specs: list[ToolSpec] = []
        tool_owner: dict[str, ToolProvider] = {}
        tool_spec_by_name: dict[str, ToolSpec] = {}
        for provider in providers:
            for spec in provider.get_tool_specs():
                tool_specs.append(spec)
                tool_owner[spec.name] = provider
                tool_spec_by_name[spec.name] = spec

        if llm_client is not None:
            llm = llm_client
        else:
            known_folders = resolve_known_folders()
            facts = "KNOWN LOCATIONS ON THIS MACHINE (use these exact paths verbatim for \"Desktop\"/\"Documents\"/" \
                "\"home folder\" -- never guess a 'C:\\Users\\<name>\\...' path yourself, since redirected " \
                "folders like OneDrive Desktop won't match a guess and will fail with a permission or not-found " \
                "error):\n" + "\n".join(f"- {label}: {path}" for label, path in known_folders.items())
            llm = LLMClient.from_config(config, tool_specs, facts)
            if config.decider == "hybrid":
                # Jev picks browser click/type/scroll steps; the Claude client
                # just built decides everything else. See jev.py.
                from jev import JevDecider, shared_client

                llm = JevDecider(
                    llm, shared_client(config.typesafe_api_key, config.typesafe_model),
                    min_confidence=config.jev_min_confidence,
                    windows_listing=(lambda: windows_session.last_listing) if windows_session else None,
                    windows_min_confidence=config.jev_min_confidence_windows,
                )

        for step in range(1, config.max_steps + 1):
            steps_taken = step
            _check_stop(should_stop)
            if task_updates is not None:
                for update in task_updates():
                    user_updates.append(update)
                    logger.note(f"Message from the user mid-task: {update!r}")
            timing: dict = {"step": step}
            step_timings.append(timing)
            # OBSERVE only applies to the browser arm. Before the browser
            # has been used at all (session.page is None -- e.g. an
            # Excel-only task, or a mixed task that hasn't reached a
            # browser action yet), there's nothing to observe; llm.py
            # handles observation=None by saying so in the prompt instead
            # of crashing on it.
            if session.page is None:
                observation = None
            else:
                try:
                    started = time.perf_counter()
                    observation = session.observe(max_chars=config.max_dom_chars)
                    timing["observe_ms"] = round((time.perf_counter() - started) * 1000, 1)
                except Exception as e:
                    raise TaskCannotBeCompleted(
                        explain(
                            f"Could not read the current page ({session.page.url}).",
                            "The page may still be loading, or it uses an unusual structure Playwright can't parse.",
                            "Try increasing STEP_TIMEOUT_MS in .env, or simplify the task.",
                        )
                    ) from e

            # --- VERIFY: check the effect of the PREVIOUS step's action,
            # now that this fresh OBSERVE has happened, before deciding
            # anything new. Delegated to the owning provider's verify() --
            # see BrowserToolProvider.verify() in browser.py. ---
            if pending_verify is not None and observation is not None:
                warning = pending_verify["provider"].verify(
                    pending_verify["action"], pending_verify["args"], pending_verify["pre_state"], observation
                )
                if warning:
                    verification_warnings.append(warning)
                    consecutive_no_effect_actions += 1
                    logger.note(f"VERIFY: {warning}")
                    print(f"  [verify] (!) {warning}")
                    if history:
                        history[-1] += f" [VERIFY: {warning}]"

                    # Give the model one chance to self-correct before
                    # giving up entirely -- see CONSECUTIVE_NO_EFFECT_*
                    # thresholds' comment. Different actions each having no
                    # effect (e.g. clicking several unrelated elements in
                    # turn) never trips the exact-repeat guard below, so
                    # this is the only thing that would ever catch it.
                    if consecutive_no_effect_actions >= CONSECUTIVE_NO_EFFECT_ABORT_THRESHOLD:
                        raise TaskCannotBeCompleted(
                            explain(
                                f"The agent made no observable progress for "
                                f"{consecutive_no_effect_actions} consecutive actions.",
                                "Each of the last several actions was expected to change the page but "
                                "didn't -- the model may be targeting the wrong elements, or the page "
                                "may not be responding the way it appears to.",
                                "Try rephrasing the task to be more specific about what to interact "
                                "with, or check that the page behaves as expected outside the agent.",
                            )
                        )
                    if consecutive_no_effect_actions == CONSECUTIVE_NO_EFFECT_HINT_THRESHOLD and history:
                        hint = (
                            "your last few actions had no observable effect -- try a different element, "
                            "a different tool, or reconsider your approach rather than repeating similar actions."
                        )
                        history[-1] += f" [HINT: {hint}]"
                        logger.note(f"HINT: {hint}")
                        print(f"  [hint] {hint}")
                else:
                    consecutive_no_effect_actions = 0
                pending_verify = None
            else:
                consecutive_no_effect_actions = 0

            if observation is not None and observation.looks_like_login and step > 1:
                logger.note(f"Login/authentication wall detected at {observation.url}")
                wall_offer_attempts += 1
                if offer_manual_resolution(observation.url, config, dry_run, logger, wall_offer_attempts, handoff):
                    continue
                raise TaskCannotBeCompleted(
                    explain(
                        f"The page at {observation.url} appears to require login "
                        "(or shows a CAPTCHA/verification prompt).",
                        "This site needs authentication that Phase 1 does not attempt to bypass "
                        "(by design -- see README section 3).",
                        "Log in manually in the Chrome profile this agent uses "
                        f"({config.chrome_user_data_dir}), then re-run the task.",
                    )
                )

            try:
                started = time.perf_counter()
                decision = llm.decide_next_action(task_now(), history, observation)
                timing["decide_ms"] = round((time.perf_counter() - started) * 1000, 1)
            except LLMError as e:
                raise TaskCannotBeCompleted(_explain_llm_error(e)) from e

            action = decision.get("action", "")
            timing["action"] = action
            if "decider" in decision:  # only set by jev.JevDecider (DECIDER=hybrid)
                timing["decider"] = decision["decider"]
                if decision.get("escalation_reason"):
                    logger.note(f"Claude decided this step: {decision['escalation_reason']}")
            args = decision.get("args", {}) or {}
            thought = decision.get("thought", "")
            current_url = observation.url if observation is not None else "(no browser page open)"
            logger.action(step, thought, action, args, current_url)
            print(f"\nStep {step}: {thought}")
            print(f"  -> {action} {args}")
            if on_step is not None:
                try:
                    on_step(step, thought, action)  # e.g. voice.py's window; display only
                except Exception:  # noqa: BLE001 -- a display glitch must never break the task
                    pass

            # --- stuck-loop / cost guard: bail out if the model repeats the
            # exact same action three times in a row, or oscillates between
            # two different actions (A, B, A, B) -- the second catches a
            # model bouncing between two elements/approaches without ever
            # trying a third, which the exact-repeat check alone would miss
            # since no single action repeats three times running.
            # "finish" is excluded here because a repeated empty-summary
            # finish is a distinct failure mode with its own guard below. ---
            history.append(f"{action} {args} -> thought: {thought}")
            recent3 = [h.split(" -> thought:")[0] for h in history[-3:]]
            recent4 = [h.split(" -> thought:")[0] for h in history[-4:]]
            exact_repeat = len(history) >= 3 and len(set(recent3)) == 1
            oscillating = (
                len(history) >= 4 and recent4[0] == recent4[2] and recent4[1] == recent4[3]
                and recent4[0] != recent4[1]
            )
            # A three-action cycle run twice (A, B, C, A, B, C) is the same
            # kind of stuck: a voice "take a screenshot" clicked the Snipping
            # Tool overlay, listed windows, listed controls, and went round
            # again until MAX_STEPS.
            recent6 = [h.split(" -> thought:")[0] for h in history[-6:]]
            oscillating = oscillating or (
                len(history) >= 6 and recent6[:3] == recent6[3:] and len(set(recent6[:3])) == 3
            )
            if action != "finish" and (exact_repeat or oscillating):
                raise TaskCannotBeCompleted(
                    explain(
                        "The agent repeated the same action three times without progress." if exact_repeat
                        else "The agent is oscillating between the same few actions without progress.",
                        "The AI model may be stuck (e.g. the page didn't change as expected, "
                        "or the element index it picked doesn't do what it thinks).",
                        "Try rephrasing the task to be more specific, or increase MAX_STEPS "
                        "if the task genuinely needs more room.",
                    )
                )

            if action == "login_required":
                logger.note(f"Model reported login_required at {current_url}: {args.get('reason', '')}")
                wall_offer_attempts += 1
                if offer_manual_resolution(current_url, config, dry_run, logger, wall_offer_attempts, handoff):
                    continue
                raise TaskCannotBeCompleted(
                    explain(
                        f"Manual login is required at {current_url}.",
                        args.get("reason", "The model detected an authentication requirement."),
                        f"Log in manually in the Chrome profile this agent uses "
                        f"({config.chrome_user_data_dir}), then re-run the task.",
                    )
                )

            if action == "finish":
                result_summary = (args.get("summary") or "").strip()
                if not result_summary:
                    empty_finish_attempts += 1
                    if empty_finish_attempts >= 3:
                        raise TaskCannotBeCompleted(
                            explain(
                                "The AI model finished the task 3 times without ever writing a summary.",
                                "It located/completed the requested page action but kept refusing to "
                                "report what it found, despite being asked to try again each time.",
                                "Try a more specific task (e.g. name exactly what to extract or report), "
                                "or try a different LLM_MODEL in .env.",
                            )
                        )
                    logger.note(f"Model called finish without a summary (attempt {empty_finish_attempts}/3); "
                                "asking it to try again.")
                    history[-1] += (
                        " [REJECTED: empty summary. Look at the VISIBLE TEXT above and write 2-3 sentences "
                        "reporting the actual information/results found there -- not a status confirmation.]"
                    )
                    continue
                break

            if action == "extract":
                # The model already has the visible text; nothing to execute.
                continue

            _check_stop(should_stop)  # the decision above can take seconds; honor a stop pressed meanwhile
            act_started = time.perf_counter()
            confirm_wait["ms"] = 0.0
            try:
                try:
                    result_text = _dispatch_action(
                        tool_owner, tool_spec_by_name, action, args, config, dry_run, confirm
                    )
                finally:
                    elapsed = (time.perf_counter() - act_started) * 1000
                    timing["act_ms"] = round(elapsed - confirm_wait["ms"], 1)
                    if confirm_wait["ms"]:
                        timing["confirm_wait_ms"] = round(confirm_wait["ms"], 1)
            except IndexError as e:
                logger.error(str(e))
                history[-1] += " [FAILED: invalid element index]"
                continue
            except TaskCannotBeCompleted:
                # A declined sensitive-action confirmation raises this from
                # inside _dispatch_action -- it must stop the task (see
                # README section 4), not be swallowed as a generic
                # per-action failure by the broader except below.
                raise
            except Exception as e:
                # Catches ExcelError (bad path/sheet/cell, file locked, ...)
                # as well as any other action-execution failure.
                logger.error(f"Action '{action}' failed: {e}")
                history[-1] += f" [FAILED: {e}]"
                continue

            # excel_* actions return a description of what happened (there's
            # no "observe" step for them to be picked up by otherwise) --
            # put it straight into the action's own history entry so the
            # model sees it on the next turn. Browser actions return None
            # here; their effect is picked up by the next OBSERVE + VERIFY.
            if result_text:
                history[-1] += f" [RESULT: {result_text}]"

            # Record what this action actually touched, for the structured
            # output file (see _save_output) -- a lightweight audit trail,
            # not something the loop's own decisions depend on.
            if action == "goto":
                artifacts.append({"type": "url_visited", "url": args.get("url", "")})
            elif action == "excel_open":
                artifacts.append({"type": "excel_file_opened", "path": str(excel_session.path)})
            elif action == "excel_save":
                artifacts.append({"type": "excel_file_saved", "path": str(excel_session.path)})
            elif tool_owner.get(action) in mcp_providers:
                artifacts.append({"type": "mcp_tool_call", "tool": action, "args": args})

            # ACT succeeded without raising -- schedule the VERIFY check for
            # the top of the next loop iteration, once we have a fresh
            # OBSERVE to compare against. Delegated to the owning provider's
            # wants_verification() (see BrowserToolProvider.wants_verification
            # -- excel actions never opt in, they're deterministic and
            # self-reporting). Nothing to compare against yet if this was
            # the very first browser action (observation was None going
            # into this step).
            if observation is not None and tool_owner[action].wants_verification(action, args):
                pending_verify = {
                    "provider": tool_owner[action], "action": action, "args": args,
                    "pre_state": {
                        "url": observation.url, "text": observation.visible_text,
                        "state": observation.state_fingerprint,
                    },
                }
        else:
            raise TaskCannotBeCompleted(
                explain(
                    f"The task did not finish within MAX_STEPS ({config.max_steps}).",
                    "The task may be more complex than the step budget allows, "
                    "or the agent took inefficient actions.",
                    "Increase MAX_STEPS in .env, or break the task into smaller steps.",
                )
            )

    except TaskCannotBeCompleted as e:
        error_message = str(e)
        logger.error(error_message)
    finally:
        if browser_session is None:
            session.stop()  # a caller-owned session stays open (see browser_session above)
        excel_session.close()
        if hasattr(llm, "close"):
            llm.close()
        for provider in mcp_providers:
            provider.close()

    output_path = _save_output(
        task=task,
        status="failed" if error_message else "success",
        summary=error_message or result_summary or "(empty result)",
        steps_taken=steps_taken,
        artifacts=artifacts,
        verification_warnings=verification_warnings,
        # llm can still be None if provider/tool-spec construction itself
        # raised before it was ever assigned (e.g. an MCP server failed to
        # start) -- no LLM calls happened in that case, so all zeros is the
        # honest answer, not a missing one.
        token_usage=llm.get_usage() if llm is not None else dict(ZERO_USAGE),
        timings=summarize_timings(step_timings),
    )

    if error_message:
        logger.finish(f"FAILED\n{error_message}")
        return {"success": False, "result": error_message, "output_path": str(output_path)}

    logger.finish(result_summary or "(empty result)")
    return {"success": True, "result": result_summary, "output_path": str(output_path)}


def _explain_llm_error(error: Exception) -> str:
    """A plain explanation for the common, fixable LLM failures. Found on the
    user's PC: an exhausted API credit balance was reported as "could not be
    reached... check your API key and internet connection", which sent them
    looking in the wrong place -- and in voice mode only that headline is
    spoken. Matches the SDKs' "Error code: NNN" status, not stray digits
    (request ids contain numbers too)."""
    text = str(error)
    lowered = text.lower()
    status = re.search(r"error code: (\d{3})", lowered)
    code = status.group(1) if status else ""
    if code == "402" or any(s in lowered for s in (
            "credit balance", "billing", "insufficient_quota", "insufficient balance")):
        return explain(
            "Your AI provider account has run out of credit.",
            text,
            "Add credit in your provider's billing settings (Anthropic: console.anthropic.com, Settings > "
            "Billing; DeepSeek: platform.deepseek.com; OpenAI: platform.openai.com), then try again. Voice "
            "quick commands keep working meanwhile.",
        )
    if code in ("401", "403") or "authentication" in lowered or "invalid x-api-key" in lowered:
        return explain(
            "Your AI provider rejected the API key.",
            text,
            "Check the API key for your LLM_PROVIDER in .env (no extra spaces or quotes).",
        )
    if code in ("400", "404") and "model" in lowered and any(s in lowered for s in (
            "supported", "not found", "does not exist", "invalid model", "unknown model")):
        return explain(
            "Your AI provider doesn't know the model name in LLM_MODEL.",
            text,
            "Set LLM_MODEL in .env to a name this provider lists (the WHY line above often names them).",
        )
    if code in ("429", "529") or "overloaded" in lowered or "rate_limit" in lowered:
        return explain(
            "The AI service is busy right now.",
            text,
            "Wait a minute and try again.",
        )
    return explain(
        "The AI model could not be reached or gave an unusable reply.",
        text,
        "Check your API key and LLM_PROVIDER/LLM_MODEL in .env, and your internet connection.",
    )


def _check_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop is not None and should_stop():
        raise TaskCannotBeCompleted(
            explain(
                "Stopped by you.",
                "The stop key was pressed, so no further actions were taken.",
                "Anything already done before the stop (e.g. a page opened) stays as it is.",
            )
        )


def _dispatch_action(
    tool_owner: dict[str, ToolProvider], tool_spec_by_name: dict[str, ToolSpec], action: str, args: dict,
    config, dry_run: bool, confirm: Callable[[str], bool],
) -> str | None:
    """
    Dispatches one action to whichever ToolProvider owns it -- the generic
    replacement for the old per-arm hardcoded dispatch (`if
    action.startswith("excel_")` plus separate confirmation logic per
    browser action). Works the same for any current or future arm without
    this function needing to know which one `action` belongs to; see
    tool_provider.py for the ToolProvider contract.

    Browser actions return None (their effect is picked up by the next
    OBSERVE + VERIFY instead); excel_* actions return a short result string
    that the caller puts straight into the action's own history entry,
    since there's no equivalent "observe the whole environment" step for a
    spreadsheet.

    `confirm` is run_task()'s resolved confirm_callback (ask_confirmation by
    default, or whatever the caller passed in) -- the one [y/n] gate below
    goes through it instead of calling ask_confirmation()/input() directly,
    so a non-terminal front-end can ask its own way.
    """
    provider = tool_owner.get(action)
    if provider is None:
        raise ValueError(f"Unknown action from model: {action!r}")

    # Lazily start whatever resource this arm needs (e.g. Chrome on the
    # first browser action) before deciding risk or executing -- a no-op
    # for arms with nothing to start, or once already started.
    provider.ensure_ready()

    risk = provider.get_dynamic_risk(action, args) or tool_spec_by_name[action].risk_level
    if not dry_run and requires_confirmation(risk, config):
        desc = provider.describe_for_confirmation(action, args)
        if not confirm(f"Ready to {desc}. Continue?"):
            raise TaskCannotBeCompleted(
                explain(
                    "A sensitive action was not confirmed, so it was not done.",
                    f"{desc[0].upper()}{desc[1:]} was flagged for confirmation and declined "
                    "(the answer was no, or no clear yes was heard).",
                    "Re-run the task and confirm the action if it was actually intended.",
                )
            )

    return provider.execute(action, args)


def summarize_timings(step_timings: list[dict]) -> dict:
    """Per-step timings plus the medians the Phase 0 baseline compares
    (docs/JEV_VOICE_PLAN.md). A median is None when no step reached that
    phase (e.g. an Excel-only task never observes a page)."""
    def median(key: str) -> float | None:
        values = [t[key] for t in step_timings if key in t]
        return round(statistics.median(values), 1) if values else None

    return {
        "median_observe_ms": median("observe_ms"),
        "median_decide_ms": median("decide_ms"),
        "median_act_ms": median("act_ms"),
        "steps": step_timings,
    }


def _save_output(
    task: str, status: str, summary: str, steps_taken: int, artifacts: list[dict], verification_warnings: list[str],
    token_usage: dict[str, int], timings: dict | None = None,
) -> Path:
    """
    Structured result contract (ARCHITECTURE_DECISIONS.md section 4):
    written for every run, success or failure, not just successful ones as
    before -- so output/ is a full audit trail of what the agent actually
    did, not only a record of what it said at the end. `summary` is the
    final answer on success, or the same human-readable explanation
    returned as outcome["result"] on failure. `token_usage` is
    LLMClient.get_usage()'s input/output/cache token counts accumulated
    across every real LLM call this task made -- all zeros for a
    MockProvider-driven run, which is accurate, not a placeholder.
    `timings` is summarize_timings()'s per-step observe/decide/act ms, with
    time spent waiting on a human [y/n] answer excluded from act.
    """
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = OUTPUT_DIR / f"{stamp}.json"
    record = {
        "task": task,
        "status": status,
        "summary": summary,
        "steps_taken": steps_taken,
        "artifacts": artifacts,
        "verification_warnings": verification_warnings,
        "token_usage": token_usage,
        "timings": timings or summarize_timings([]),
        "saved_at": datetime.now().isoformat(),
    }
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Local AI browser automation agent (Phase 1).")
    parser.add_argument("task", nargs="?", help="Task to run. If omitted, you will be prompted for one.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip confirmation prompts for sensitive actions (for automated testing only).",
    )
    args = parser.parse_args()

    config = load_config()
    problems = config.validate()
    if problems:
        print("Configuration problem(s) found:")
        for p in problems:
            print(f"  - {p}")
        print("\nSee .env.example for the required settings.")
        sys.exit(1)

    task = args.task or input("Task: ").strip()
    if not task:
        print("No task given, exiting.")
        sys.exit(1)

    print(f"\nRunning task with {config.llm_provider}/{config.llm_model} "
          f"(max {config.max_steps} steps)...")
    start = time.time()
    outcome = run_task(task, config, dry_run=args.dry_run)
    elapsed = time.time() - start

    print("\n" + "=" * 60)
    if outcome["success"]:
        print("RESULT:")
        print(outcome["result"])
        print(f"\nSaved to: {outcome['output_path']}")
    else:
        print("TASK FAILED")
        print(outcome["result"])
        print(f"\nSaved to: {outcome['output_path']}")
    print(f"(took {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
