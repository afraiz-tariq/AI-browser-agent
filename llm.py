"""
Provider-agnostic LLM client.

The agent loop (agent.py) only ever calls `LLMClient.decide_next_action(...)`
and gets back a plain dict describing one action. It never knows or cares
whether that came from OpenAI, Anthropic, or a canned mock response used in
tests. To switch models later you only need to change LLM_PROVIDER /
LLM_MODEL in .env -- no code changes.

Each real action (goto, click, type, ...) is exposed to the model as a
separate native tool ("function calling" / "tool use"), rather than asking
the model to hand-write one JSON blob in free text. This matters in
practice: a model can decide it's "ready to summarize" and then still emit
an empty args object for finish, because writing a long "summary" string
correctly inside a bigger JSON object it's composing character-by-character
is a harder generation task than filling in one declared, required tool
parameter. Native tool calling constrains generation to the declared schema
(so a required field like finish's "summary" is enforced as part of
sampling, not hoped for), and both providers implement it the same way from
the agent loop's point of view.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any


class LLMError(Exception):
    """Raised when the LLM cannot be reached or returns something unusable."""


SYSTEM_PROMPT = """You are the reasoning engine of a browser automation agent.
You are given a TASK, a short ACTION HISTORY, and an OBSERVATION of the
current webpage (its URL, title, a numbered list of interactive elements,
and a snippet of visible text).

Call exactly one of the provided tools to choose the single next action.
You must call a tool on every turn -- there is no plain-text reply.

Rules:
- Every tool call includes "thought": one short sentence on what you see and
  why you're doing this. Keep it brief to save tokens.
- Only use element indices that appear in the CURRENT observation. They
  change on every page, so never reuse an index from an earlier step.
- Prefer the simplest path to the goal. Do not repeat an action that already
  failed or had no visible effect -- try something different instead.
- If the page shows a login form, a "sign in to continue" wall, a CAPTCHA,
  or 2FA/MFA prompt, call login_required immediately. Never try to guess
  credentials, solve a CAPTCHA, or bypass MFA.
- finish's "summary" must always contain real content from the page, never
  a status confirmation. "Search results for X are displayed" or "Task
  complete" are NOT valid summaries and will be rejected -- read the
  VISIBLE TEXT in the observation and report the actual information it
  contains (e.g. the titles/snippets of the top results, the fact(s) found,
  the data extracted). Even if the task only asked you to perform an action
  (like "search for X") rather than asking a question, still summarize what
  the results actually show -- that IS the useful output of the task.
"""

# One entry per action the agent loop understands (see agent.py's
# _execute_action). Each becomes a separate tool/function so the API itself
# enforces the required fields -- e.g. it's not possible to "call finish"
# without also generating a non-empty "summary" string, because that's a
# required parameter of the finish tool, not a key in a hand-written blob.
ACTION_SPECS: dict[str, dict[str, Any]] = {
    "goto": {
        "description": "Navigate the browser to an absolute URL.",
        "properties": {"url": {"type": "string", "description": "Absolute URL to navigate to."}},
        "required": ["url"],
    },
    "click": {
        "description": "Click an interactive element from the CURRENT observation.",
        "properties": {"index": {"type": "integer", "description": "Element index from the CURRENT observation."}},
        "required": ["index"],
    },
    "type": {
        "description": "Type text into an input/textarea element from the CURRENT observation, "
                        "optionally submitting it.",
        "properties": {
            "index": {"type": "integer", "description": "Element index from the CURRENT observation."},
            "text": {"type": "string", "description": "Text to type into the element."},
            "submit": {"type": "boolean", "description": "Press Enter after typing to submit the form."},
        },
        "required": ["index", "text"],
    },
    "scroll": {
        "description": "Scroll the page up or down to reveal more content.",
        "properties": {"direction": {"type": "string", "enum": ["up", "down"]}},
        "required": ["direction"],
    },
    "go_back": {
        "description": "Go back to the previous page in browser history.",
        "properties": {},
        "required": [],
    },
    "wait": {
        "description": "Wait for a page to finish loading or settle before observing it again.",
        "properties": {"ms": {"type": "integer", "description": "Milliseconds to wait (default 1000)."}},
        "required": [],
    },
    "extract": {
        "description": "Use when the CURRENT page's visible text already contains what's needed to answer "
                        "the task. No browser action is taken; the loop just re-observes on the next step.",
        "properties": {},
        "required": [],
    },
    "finish": {
        "description": "Call this ONLY when ready to give the final answer. 'summary' must contain the "
                        "actual information/results found (specific facts, names, figures, or extracted "
                        "text) -- never a status confirmation like 'task complete'.",
        "properties": {
            "summary": {
                "type": "string",
                "description": "The complete, self-contained final answer for the user, written in full "
                                "sentences, containing real content from the page(s) visited.",
            }
        },
        "required": ["summary"],
    },
    "login_required": {
        "description": "Call this if the page shows a login form, CAPTCHA, or MFA/2FA prompt that must "
                        "not be bypassed.",
        "properties": {"reason": {"type": "string", "description": "Why login/verification appears to be required."}},
        "required": ["reason"],
    },
}

_THOUGHT_PROPERTY = {"thought": {"type": "string", "description": "One short sentence: what you see and why."}}


def _input_schema(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**_THOUGHT_PROPERTY, **spec["properties"]},
        "required": ["thought", *spec["required"]],
    }


def _anthropic_tools() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": spec["description"], "input_schema": _input_schema(spec)}
        for name, spec in ACTION_SPECS.items()
    ]


def _openai_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": name, "description": spec["description"], "parameters": _input_schema(spec)},
        }
        for name, spec in ACTION_SPECS.items()
    ]


class BaseLLMProvider(ABC):
    @abstractmethod
    def decide(self, system: str, user: str) -> dict[str, Any]:
        """Send one turn and return {"action": ..., "thought": ..., "args": {...}}."""


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI  # imported lazily so `mock`/tests don't need the package configured

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._tools = _openai_tools()

    def decide(self, system: str, user: str) -> dict[str, Any]:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tools=self._tools,
                tool_choice="required",
                temperature=0,
            )
        except Exception as e:  # network errors, auth errors, rate limits, etc.
            raise LLMError(f"OpenAI request failed: {e}") from e

        tool_calls = response.choices[0].message.tool_calls or []
        if not tool_calls:
            raise LLMError("Model did not call a tool.")
        call = tool_calls[0]
        try:
            tool_input = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError as e:
            raise LLMError(f"Model returned invalid tool arguments: {e}") from e
        thought = tool_input.pop("thought", "")
        return {"action": call.function.name, "thought": thought, "args": tool_input}


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str):
        import anthropic  # imported lazily, same reasoning as above

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._tools = _anthropic_tools()

    def decide(self, system: str, user: str) -> dict[str, Any]:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=self._tools,
                tool_choice={"type": "any"},
            )
        except Exception as e:
            raise LLMError(f"Anthropic request failed: {e}") from e

        for block in response.content:
            if block.type == "tool_use":
                tool_input = dict(block.input)
                thought = tool_input.pop("thought", "")
                return {"action": block.name, "thought": thought, "args": tool_input}
        raise LLMError("Model did not call a tool.")


class MockProvider(BaseLLMProvider):
    """
    Returns pre-scripted replies. Used by the test suite and by
    `python agent.py --dry-run` so the full agent loop (observation ->
    prompt -> parsing -> action execution -> logging) can be exercised
    without spending API credits or requiring network access.

    Each scripted reply can be either an already-built dict (the same shape
    `decide()` returns: {"action", "thought", "args"}) or a JSON string of
    the same shape, for convenience in tests.
    """

    def __init__(self, scripted_replies: list[dict[str, Any] | str] | None = None):
        self._replies = list(scripted_replies or [])
        self.calls: list[tuple[str, str]] = []

    def decide(self, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        if not self._replies:
            raise LLMError("MockProvider ran out of scripted replies.")
        raw = self._replies.pop(0)
        return json.loads(raw) if isinstance(raw, str) else raw


class LLMClient:
    def __init__(self, provider: BaseLLMProvider):
        self._provider = provider

    @classmethod
    def from_config(cls, config) -> "LLMClient":
        if config.llm_provider == "openai":
            return cls(OpenAIProvider(config.openai_api_key, config.llm_model))
        if config.llm_provider == "anthropic":
            return cls(AnthropicProvider(config.anthropic_api_key, config.llm_model))
        if config.llm_provider == "mock":
            return cls(MockProvider())
        raise LLMError(f"Unknown LLM_PROVIDER: {config.llm_provider}")

    def decide_next_action(self, task: str, history: list[str], observation) -> dict[str, Any]:
        elements_text = "\n".join(
            f"[{el.index}] <{el.tag}{'/' + el.input_type if el.input_type else ''}> {el.text!r}"
            for el in observation.elements
        ) or "(no interactive elements found)"

        history_text = "\n".join(f"- {h}" for h in history[-8:]) or "(none yet, this is the first step)"

        user_prompt = f"""TASK: {task}

ACTION HISTORY (most recent last):
{history_text}

OBSERVATION:
URL: {observation.url}
TITLE: {observation.title}
LOOKS LIKE LOGIN PAGE: {observation.looks_like_login}
INTERACTIVE ELEMENTS:
{elements_text}

VISIBLE TEXT (truncated):
{observation.visible_text}
"""
        action = self._provider.decide(SYSTEM_PROMPT, user_prompt)
        if "action" not in action:
            raise LLMError(f"Model reply is missing the 'action' field: {action}")
        action.setdefault("args", {})
        return action
