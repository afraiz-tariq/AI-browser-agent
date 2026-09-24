"""
Very small plain-text logger: one file per task run under logs/.

Deliberately not using Python's `logging` module's full config system --
for a Phase 1 prototype a plain append-only text file that you can open in
Notepad is easier to inspect than a configured logger, and it makes the
"never log secrets" requirement easy to audit by eye.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# Redact anything that looks like a key/token/password so a stray value
# never ends up on disk, even if a future code path passes one in by mistake.
#
# The sk- pattern has NO capturing group, unlike the other three -- it
# matches a bare secret with no "label:" prefix to preserve, so the whole
# match must be replaced (see _redact()'s `if m.lastindex` branch below).
# Wrapping the whole pattern in parens here was a real bug: that makes it
# group 1, so the "keep group 1, redact the rest" branch below fired and
# reproduced the entire secret verbatim with "[REDACTED]" uselessly
# appended after it -- the opposite of redaction. The character class also
# has to include "_" and "-": real Anthropic keys look like
# "sk-ant-api03-<base64url>-<checksum>", and a class of only [A-Za-z0-9]
# stops matching at the first hyphen, missing the key almost entirely.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),  # Google (Gemini) API keys
    re.compile(r"(api[_-]?key\s*[:=]\s*)\S+", re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*)\S+", re.IGNORECASE),
    re.compile(r"(authorization:\s*bearer\s+)\S+", re.IGNORECASE),
]


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]" if m.lastindex else "[REDACTED]", text)
    return text


class TaskLogger:
    def __init__(self, logs_dir: Path, task: str):
        logs_dir.mkdir(exist_ok=True)
        self.started_at = datetime.now()
        stamp = self.started_at.strftime("%Y-%m-%d_%H%M%S")
        self.path = logs_dir / f"{stamp}.log"
        self._fh = open(self.path, "a", encoding="utf-8")
        self._write(f"TASK: {task}")
        self._write(f"START TIME: {self.started_at.isoformat(timespec='seconds')}")
        self._write("-" * 60)

    def _write(self, line: str) -> None:
        self._fh.write(_redact(line) + "\n")
        self._fh.flush()

    def action(self, step: int, thought: str, action: str, args: dict, url: str) -> None:
        self._write(f"[step {step}] url={url}")
        self._write(f"[step {step}] thought: {thought}")
        self._write(f"[step {step}] action: {action} args={args}")

    def note(self, message: str) -> None:
        self._write(f"NOTE: {message}")

    def error(self, message: str) -> None:
        self._write(f"ERROR: {message}")

    def finish(self, result: str) -> None:
        ended_at = datetime.now()
        duration = (ended_at - self.started_at).total_seconds()
        self._write("-" * 60)
        self._write(f"END TIME: {ended_at.isoformat(timespec='seconds')} (duration: {duration:.1f}s)")
        self._write(f"FINAL RESULT:\n{result}")
        self._fh.close()
