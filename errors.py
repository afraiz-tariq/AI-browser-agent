"""
Shared error type and message formatter used across the agent loop
(agent.py) and its tool providers (browser.py, excel_tools.py, ...).

Split into its own module so a provider can raise a hard task-stopping
error (e.g. "Chrome could not be launched") without browser.py needing to
import agent.py -- that would be circular, since agent.py imports the
providers.
"""
from __future__ import annotations


class TaskCannotBeCompleted(Exception):
    """Raised to stop the agent loop early with a clear, user-facing explanation."""


def explain(problem: str, likely_cause: str, suggestion: str) -> str:
    return f"WHAT HAPPENED: {problem}\nWHY: {likely_cause}\nWHAT YOU CAN DO: {suggestion}"
