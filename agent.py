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
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from browser import BrowserSession, BrowserToolProvider
from config import OUTPUT_DIR, load_config
from errors import TaskCannotBeCompleted, explain
from excel_tools import ExcelSession, ExcelToolProvider
from llm import LLMClient, LLMError
from logger import TaskLogger
from mcp_tools import MCPToolProvider, build_brave_search_provider, build_fetch_provider
from tool_provider import ToolProvider, ToolSpec, requires_confirmation


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


def offer_manual_resolution(url: str, config, dry_run: bool, logger: TaskLogger, attempt: int) -> bool:
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
    """
    logger = TaskLogger(Path(__file__).parent / "logs", task)
    confirm = confirm_callback or ask_confirmation
    session = BrowserSession(config)  # Chrome itself isn't launched until first use -- see BrowserToolProvider.ensure_ready
    excel_session = ExcelSession()
    mcp_providers: list[MCPToolProvider] = []  # only non-empty per ENABLE_MCP_* flags -- closed in the finally below

    history: list[str] = []
    result_summary = None
    error_message = None
    empty_finish_attempts = 0
    wall_offer_attempts = 0
    steps_taken = 0
    pending_verify: dict | None = None
    llm = None
    # Structured result contract (see ARCHITECTURE_DECISIONS.md section 4):
    # a record of what the task actually touched, saved to output/ for
    # every run -- not just successful ones -- alongside the bare
    # success/result dict this function returns (which stays exactly as
    # it was, since discord_bot.py and the tests depend on that shape).
    artifacts: list[dict] = []
    verification_warnings: list[str] = []

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
        providers.extend(mcp_providers)

        tool_specs: list[ToolSpec] = []
        tool_owner: dict[str, ToolProvider] = {}
        tool_spec_by_name: dict[str, ToolSpec] = {}
        for provider in providers:
            for spec in provider.get_tool_specs():
                tool_specs.append(spec)
                tool_owner[spec.name] = provider
                tool_spec_by_name[spec.name] = spec

        llm = llm_client or LLMClient.from_config(config, tool_specs)

        for step in range(1, config.max_steps + 1):
            steps_taken = step
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
                    observation = session.observe(max_chars=config.max_dom_chars)
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
                    logger.note(f"VERIFY: {warning}")
                    print(f"  [verify] (!) {warning}")
                    if history:
                        history[-1] += f" [VERIFY: {warning}]"
                pending_verify = None

            if observation is not None and observation.looks_like_login and step > 1:
                logger.note(f"Login/authentication wall detected at {observation.url}")
                wall_offer_attempts += 1
                if offer_manual_resolution(observation.url, config, dry_run, logger, wall_offer_attempts):
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
                decision = llm.decide_next_action(task, history, observation)
            except LLMError as e:
                raise TaskCannotBeCompleted(
                    explain(
                        "The AI model could not be reached or gave an unusable reply.",
                        str(e),
                        "Check your API key and LLM_PROVIDER/LLM_MODEL in .env, and your internet connection.",
                    )
                ) from e

            action = decision.get("action", "")
            args = decision.get("args", {}) or {}
            thought = decision.get("thought", "")
            current_url = observation.url if observation is not None else "(no browser page open)"
            logger.action(step, thought, action, args, current_url)
            print(f"\nStep {step}: {thought}")
            print(f"  -> {action} {args}")

            # --- stuck-loop / cost guard: bail out if the model repeats
            # the exact same action three times in a row (Phase 1 keeps
            # this simple rather than trying to be clever about "progress").
            # "finish" is excluded here because a repeated empty-summary
            # finish is a distinct failure mode with its own guard below. ---
            history.append(f"{action} {args} -> thought: {thought}")
            recent = [h.split(" -> thought:")[0] for h in history[-3:]]
            if action != "finish" and len(history) >= 3 and len(set(recent)) == 1:
                raise TaskCannotBeCompleted(
                    explain(
                        "The agent repeated the same action three times without progress.",
                        "The AI model may be stuck (e.g. the page didn't change as expected, "
                        "or the element index it picked doesn't do what it thinks).",
                        "Try rephrasing the task to be more specific, or increase MAX_STEPS "
                        "if the task genuinely needs more room.",
                    )
                )

            if action == "login_required":
                logger.note(f"Model reported login_required at {current_url}: {args.get('reason', '')}")
                wall_offer_attempts += 1
                if offer_manual_resolution(current_url, config, dry_run, logger, wall_offer_attempts):
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

            try:
                result_text = _dispatch_action(
                    tool_owner, tool_spec_by_name, action, args, config, dry_run, confirm
                )
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
        session.stop()
        excel_session.close()
        for provider in mcp_providers:
            provider.close()

    output_path = _save_output(
        task=task,
        status="failed" if error_message else "success",
        summary=error_message or result_summary or "(empty result)",
        steps_taken=steps_taken,
        artifacts=artifacts,
        verification_warnings=verification_warnings,
    )

    if error_message:
        logger.finish(f"FAILED\n{error_message}")
        return {"success": False, "result": error_message, "output_path": str(output_path)}

    logger.finish(result_summary or "(empty result)")
    return {"success": True, "result": result_summary, "output_path": str(output_path)}


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
                    "User declined a sensitive action.",
                    f"{desc[0].upper()}{desc[1:]} was flagged for confirmation and declined.",
                    "Re-run the task and confirm the action if it was actually intended.",
                )
            )

    return provider.execute(action, args)


def _save_output(
    task: str, status: str, summary: str, steps_taken: int, artifacts: list[dict], verification_warnings: list[str],
) -> Path:
    """
    Structured result contract (ARCHITECTURE_DECISIONS.md section 4):
    written for every run, success or failure, not just successful ones as
    before -- so output/ is a full audit trail of what the agent actually
    did, not only a record of what it said at the end. `summary` is the
    final answer on success, or the same human-readable explanation
    returned as outcome["result"] on failure.
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
