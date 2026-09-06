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

from browser import BrowserSession
from config import OUTPUT_DIR, load_config
from llm import LLMClient, LLMError
from logger import TaskLogger

# Actions the agent must never perform without the user explicitly saying
# yes first. This is a hard safety net independent of whatever the LLM
# *thinks* is fine -- see README section 4 ("Safety").
ALWAYS_CONFIRM_ACTIONS = {"type"}  # confirmed only when submit=True or text targets a sensitive field

# Actions worth VERIFYING after they run -- these are the ones expected to
# visibly change the page (a new URL, different content, or both). Actions
# like "scroll" or a plain "type" without submitting don't reliably change
# either signal, so checking them would just be noise.
VERIFIABLE_ACTIONS = {"goto", "click", "type", "go_back"}


class TaskCannotBeCompleted(Exception):
    """Raised to stop the loop early with a clear, user-facing explanation."""


def _explain(problem: str, likely_cause: str, suggestion: str) -> str:
    return f"WHAT HAPPENED: {problem}\nWHY: {likely_cause}\nWHAT YOU CAN DO: {suggestion}"


def ask_confirmation(prompt: str) -> bool:
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


def verify_action_effect(pending: dict, observation, logger: TaskLogger, history: list[str]) -> None:
    """
    The explicit VERIFY step of the observe -> decide -> act -> verify loop.

    We already have everything needed for this without any extra Playwright
    or LLM calls: `pending` was captured right after the action ran (the
    page state just *before* it), and `observation` is the fresh page state
    from the very next OBSERVE. If neither the URL nor the visible text
    changed after an action that was expected to change one of them (a
    navigation, a click, a submitted form), the action probably didn't do
    what the model thought -- so we say so immediately, in the action's own
    history entry, rather than silently letting the model discover this
    itself several steps later (or not at all).

    Known limitation, acceptable for Phase 1: this can only see changes
    that show up in the URL or in visible text. A click that toggles a
    checkbox's checked state, for example, looks unchanged by this check
    even though it worked -- we accept that false-negative rather than
    trying to diff the full DOM, which would add real complexity for a
    prototype-level signal.
    """
    same_url = observation.url == pending["pre_url"]
    same_text = observation.visible_text == pending["pre_text"]
    if same_url and same_text:
        note = f"no observable change after {pending['action']} {pending['args']} -- it may not have worked"
        logger.note(f"VERIFY: {note}")
        print(f"  [verify] (!) {note}")
        if history:
            history[-1] += f" [VERIFY: {note}]"


def run_task(task: str, config, dry_run: bool = False, llm_client: LLMClient | None = None) -> dict:
    """
    Runs one task end-to-end and returns a result dict. Also writes a log
    file to logs/ and, on success, a result file to output/.

    `llm_client` can be injected directly (used by the test suite to pass a
    MockProvider); normally it's built from `config`.
    """
    logger = TaskLogger(Path(__file__).parent / "logs", task)
    llm = llm_client or LLMClient.from_config(config)
    session = BrowserSession(config)

    history: list[str] = []
    result_summary = None
    error_message = None
    empty_finish_attempts = 0
    wall_offer_attempts = 0
    pending_verify: dict | None = None

    try:
        session.start()
    except Exception as e:
        msg = _explain(
            "Chrome could not be launched.",
            "Google Chrome may not be installed, or Playwright cannot find it.",
            "Install Chrome, then run 'python -m playwright install chrome' and try again.",
        )
        logger.error(f"{e}\n{msg}")
        logger.finish("FAILED: could not launch browser")
        return {"success": False, "result": msg}

    try:
        for step in range(1, config.max_steps + 1):
            try:
                observation = session.observe(max_chars=config.max_dom_chars)
            except Exception as e:
                raise TaskCannotBeCompleted(
                    _explain(
                        f"Could not read the current page ({session.page.url if session.page else 'unknown'}).",
                        "The page may still be loading, or it uses an unusual structure Playwright can't parse.",
                        "Try increasing STEP_TIMEOUT_MS in .env, or simplify the task.",
                    )
                ) from e

            # --- VERIFY: check the effect of the PREVIOUS step's action,
            # now that this fresh OBSERVE has happened, before deciding
            # anything new. See verify_action_effect()'s docstring. ---
            if pending_verify is not None:
                verify_action_effect(pending_verify, observation, logger, history)
                pending_verify = None

            if observation.looks_like_login and step > 1:
                logger.note(f"Login/authentication wall detected at {observation.url}")
                wall_offer_attempts += 1
                if offer_manual_resolution(observation.url, config, dry_run, logger, wall_offer_attempts):
                    continue
                raise TaskCannotBeCompleted(
                    _explain(
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
                    _explain(
                        "The AI model could not be reached or gave an unusable reply.",
                        str(e),
                        "Check your API key and LLM_PROVIDER/LLM_MODEL in .env, and your internet connection.",
                    )
                ) from e

            action = decision.get("action", "")
            args = decision.get("args", {}) or {}
            thought = decision.get("thought", "")
            logger.action(step, thought, action, args, observation.url)
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
                    _explain(
                        "The agent repeated the same action three times without progress.",
                        "The AI model may be stuck (e.g. the page didn't change as expected, "
                        "or the element index it picked doesn't do what it thinks).",
                        "Try rephrasing the task to be more specific, or increase MAX_STEPS "
                        "if the task genuinely needs more room.",
                    )
                )

            if action == "login_required":
                logger.note(f"Model reported login_required at {observation.url}: {args.get('reason', '')}")
                wall_offer_attempts += 1
                if offer_manual_resolution(observation.url, config, dry_run, logger, wall_offer_attempts):
                    continue
                raise TaskCannotBeCompleted(
                    _explain(
                        f"Manual login is required at {observation.url}.",
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
                            _explain(
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
                _execute_action(session, action, args, config, dry_run)
            except IndexError as e:
                logger.error(str(e))
                history[-1] += " [FAILED: invalid element index]"
                continue
            except TaskCannotBeCompleted:
                # A declined sensitive-action confirmation raises this from
                # inside _execute_action -- it must stop the task (see
                # README section 4), not be swallowed as a generic
                # per-action failure by the broader except below.
                raise
            except Exception as e:
                logger.error(f"Action '{action}' failed: {e}")
                history[-1] += f" [FAILED: {e}]"
                continue

            # ACT succeeded without raising -- schedule the VERIFY check for
            # the top of the next loop iteration, once we have a fresh
            # OBSERVE to compare against. A plain "type" that isn't
            # submitting anything isn't expected to change the URL or page
            # text, so it's excluded to avoid false alarms.
            if action in VERIFIABLE_ACTIONS and (action != "type" or args.get("submit")):
                pending_verify = {
                    "action": action, "args": args,
                    "pre_url": observation.url, "pre_text": observation.visible_text,
                }
        else:
            raise TaskCannotBeCompleted(
                _explain(
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

    if error_message:
        logger.finish(f"FAILED\n{error_message}")
        return {"success": False, "result": error_message}

    logger.finish(result_summary or "(empty result)")
    output_path = _save_output(task, result_summary or "")
    return {"success": True, "result": result_summary, "output_path": str(output_path)}


def _execute_action(session: BrowserSession, action: str, args: dict, config, dry_run: bool) -> None:
    if action == "goto":
        session.goto(args["url"])
    elif action == "click":
        index = int(args["index"])
        if config.confirm_sensitive_actions and session.is_sensitive(index) and not dry_run:
            desc = session.element_summary(index)
            if not ask_confirmation(f"Ready to click {desc}. This looks like it may have side effects. Continue?"):
                raise TaskCannotBeCompleted(
                    _explain(
                        "User declined a sensitive action.",
                        f"Clicking {desc} was flagged as potentially irreversible "
                        "(form submission, purchase, delete, etc.).",
                        "Re-run the task and confirm the action if it was actually intended.",
                    )
                )
        session.click(index)
    elif action == "type":
        index = int(args["index"])
        text = str(args.get("text", ""))
        submit = bool(args.get("submit", False))
        if config.confirm_sensitive_actions and submit and not dry_run:
            desc = session.element_summary(index)
            if not ask_confirmation(f"Ready to type into {desc} and submit. Continue?"):
                raise TaskCannotBeCompleted(
                    _explain(
                        "User declined a sensitive action.",
                        "Submitting a form was flagged for confirmation and declined.",
                        "Re-run the task and confirm if the submission was actually intended.",
                    )
                )
        session.type_text(index, text, submit=submit)
    elif action == "scroll":
        session.scroll(args.get("direction", "down"))
    elif action == "go_back":
        session.go_back()
    elif action == "wait":
        session.wait(int(args.get("ms", 1000)))
    else:
        raise ValueError(f"Unknown action from model: {action!r}")


def _save_output(task: str, result: str) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = OUTPUT_DIR / f"{stamp}.json"
    path.write_text(
        json.dumps({"task": task, "result": result, "saved_at": datetime.now().isoformat()}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
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
    print(f"(took {elapsed:.1f}s)")


if __name__ == "__main__":
    main()
