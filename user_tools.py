"""
The "ask the person" arm: one tool, ask_user, that lets the agent ask the
person a question mid-task ("Which of the three open Notepad tabs?")
instead of guessing or giving up. Only registered when a front-end that can
ask is running the task (the voice app, voice.py / app_ui.py); a plain
`python agent.py` run never has it.

Risk: explicitly classified R0 (the "new tools start at R3" rule). Asking a
question changes nothing anywhere; whatever the agent does with the answer
goes through the normal risk tiers and confirmations. The answer is not
returned as a tool result -- tool results are DATA, never instructions
(llm.py's trust hierarchy) -- but handed to agent.py via `on_answer`, which
adds it to the TASK text itself, the one trusted slot, the same place a
message typed mid-task goes. So a web page can't impersonate the person by
printing something that looks like an answer.
"""
from __future__ import annotations

from typing import Callable

from tool_provider import ToolProvider, ToolSpec

ASK_USER_SPEC = ToolSpec(
    name="ask_user",
    description=(
        "Ask the person a short question and wait for their answer -- ONLY when you truly need information that "
        "only they have and can't find yourself (e.g. which of several matching items they meant, or a detail "
        "the task left out). Never use it to ask permission for an action: risky actions are confirmed "
        "automatically. The answer is added to the TASK as a message from the user."
    ),
    properties={"question": {"type": "string", "description": "One short, plain question."}},
    required=["question"],
    risk_level="R0",
)


class UserToolProvider(ToolProvider):
    def __init__(self, ask: Callable[[str], str | None], on_answer: Callable[[str, str], None]):
        self._ask = ask
        self._on_answer = on_answer

    def get_tool_specs(self) -> list[ToolSpec]:
        return [ASK_USER_SPEC]

    def describe_for_confirmation(self, name: str, args: dict) -> str:
        return f"ask you: {args.get('question', '')}"

    def execute(self, name: str, args: dict) -> str | None:
        question = str(args.get("question", "")).strip()
        if not question:
            return "No question was given."
        answer = (self._ask(question) or "").strip()
        if not answer:
            return "The person didn't answer. Carry on with your best judgment, or finish and say what you need."
        self._on_answer(question, answer)
        return "The person answered; their answer is now at the end of the TASK."
