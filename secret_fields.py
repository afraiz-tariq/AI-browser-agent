"""
Which form fields hold secrets, so their contents never leave this machine.

Every arm turns what's on screen into text for the model (browser.py's
observe(), windows_tools.py's windows_list_controls). A field's *label*
("Password", "Card number") is fine to send -- the model needs it to know
what the field is. A secret field's *value* is not: it would reach the LLM
provider, and from there any future provider this project adds. This module
is the one shared answer to "is this field a secret?", so both arms mask the
same things.

This is the "don't send it" guard. logger.py's redaction is a separate,
later guard for log files; neither replaces the other.
"""
from __future__ import annotations

import re

# Matched against a field's label/name/id/placeholder -- never against its
# value. Deliberately broad: masking an ordinary field by mistake only costs
# the model one value it could have seen; missing a real secret field sends
# the secret away.
_SECRET_LABEL = re.compile(
    r"passw|passcode|passphrase|\bpin\b|\bcvv\b|\bcvc\b|\bcsc\b|security.?code|card.?number|credit.?card|"
    r"\bssn\b|social.?security|secret|token|api.?key|one.?time.?code|\botp\b|2fa|verification.?code",
    re.IGNORECASE,
)

# HTML autocomplete tokens that name a secret outright (per the HTML spec's
# autofill field names).
SECRET_AUTOCOMPLETE = frozenset({
    "current-password", "new-password", "one-time-code", "cc-number", "cc-csc",
})

HIDDEN = "[hidden]"


def is_secret_label(*labels: str | None) -> bool:
    """True if any of a field's descriptive attributes suggests it holds a secret."""
    return any(label and _SECRET_LABEL.search(label) for label in labels)
