"""
Provider-agnostic LLM client.

The agent loop (agent.py) only ever calls `LLMClient.decide_next_action(...)`
and gets back a plain dict describing one action. It never knows or cares
whether that came from OpenAI, Anthropic, or a canned mock response used in
tests. To switch models later you only need to change LLM_PROVIDER /
LLM_MODEL in .env -- no code changes.

We deliberately do NOT use each provider's native "function calling" /
"tool use" feature. Prompting the model to return one JSON object is simpler,
identical across providers, and easy for a human to read while learning how
the agent "thinks" -- which is the whole point of Phase 1.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any


class LLMError(Exception):
    """Raised when the LLM cannot be reached or returns something unusable."""


SYSTEM_PROMPT = """You are the reasoning engine of a browser automation agent.
You are given a TASK, a short ACTION HISTORY, and an OBSERVATION of the
current webpage (its URL, title, a numbered list of interactive elements,
and a snippet of visible text).

Reply with EXACTLY ONE JSON object (no markdown fences, no commentary
outside the JSON) describing the single next action to take:

{"thought": "<one short sentence: what you see and why you're doing this>",
 "action": "<one of: goto | click | type | scroll | go_back | wait | extract | finish | login_required>",
 "args": { ... },
 "confidence": "<low | medium | high>"}

Action argument reference:
- goto: {"url": "<absolute url>"}
- click: {"index": <element index from the observation>}
- type: {"index": <element index>, "text": "<text to enter>", "submit": <true|false>}
- scroll: {"direction": "up" | "down"}
- go_back: {}
- wait: {"ms": <milliseconds, default 1000>}
- extract: {} -- use this when the CURRENT page already contains the
  information needed to answer the task; the visible text you were given
  will be used as the source for the final answer.
- finish: {"summary": "<the final answer/result for the user, written in full sentences>"}
- login_required: {"reason": "<why you believe login/authentication is required>"}

Rules:
- Only use element indices that appear in the CURRENT observation. They
  change on every page, so never reuse an index from an earlier step.
- Prefer the simplest path to the goal. Do not repeat an action that already
  failed or had no visible effect -- try something different instead.
- If the page shows a login form, a "sign in to continue" wall, a CAPTCHA,
  or 2FA/MFA prompt, respond with "login_required" immediately. Never try to
  guess credentials, solve a CAPTCHA, or bypass MFA.
- "summary" must always contain real content from the page, never a status
  confirmation. "Search results for X are displayed" or "Task complete" are
  NOT valid summaries and will be rejected -- read the VISIBLE TEXT in the
  observation and report the actual information it contains (e.g. the
  titles/snippets of the top results, the fact(s) found, the data
  extracted). Even if the task only asked you to perform an action (like
  "search for X") rather than asking a question, still summarize what the
  results actually show -- that IS the useful output of the task.
- Keep "thought" to one short sentence to save tokens.
"""


def _extract_json(raw: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from a model reply."""
    raw = raw.strip()
    # Strip ```json ... ``` fences if the model added them anyway.
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1)
    else:
        # Fall back to the first {...} block in the text.
        brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace_match:
            raw = brace_match.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"Model did not return valid JSON: {e}\nRaw reply: {raw[:500]}")


class BaseLLMProvider(ABC):
    @abstractmethod
    def chat(self, system: str, user: str) -> str:
        """Send one turn and return the raw text reply."""


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str):
        from openai import OpenAI  # imported lazily so `mock`/tests don't need the package configured

        self._client = OpenAI(api_key=api_key)
        self._model = model

    def chat(self, system: str, user: str) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0,
            )
        except Exception as e:  # network errors, auth errors, rate limits, etc.
            raise LLMError(f"OpenAI request failed: {e}") from e
        return response.choices[0].message.content or ""


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, api_key: str, model: str):
        import anthropic  # imported lazily, same reasoning as above

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def chat(self, system: str, user: str) -> str:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:
            raise LLMError(f"Anthropic request failed: {e}") from e
        return "".join(block.text for block in response.content if block.type == "text")


class MockProvider(BaseLLMProvider):
    """
    Returns pre-scripted replies. Used by the test suite and by
    `python agent.py --dry-run` so the full agent loop (observation ->
    prompt -> parsing -> action execution -> logging) can be exercised
    without spending API credits or requiring network access.
    """

    def __init__(self, scripted_replies: list[str] | None = None):
        self._replies = list(scripted_replies or [])
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if not self._replies:
            raise LLMError("MockProvider ran out of scripted replies.")
        return self._replies.pop(0)


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
        raw = self._provider.chat(SYSTEM_PROMPT, user_prompt)
        action = _extract_json(raw)
        if "action" not in action:
            raise LLMError(f"Model reply is missing the 'action' field: {action}")
        action.setdefault("args", {})
        return action
