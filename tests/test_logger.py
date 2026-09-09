"""
Tests for logger.py's secret redaction -- a security-relevant feature
(README: "Per-task plain-text logging (with secret redaction)") that had
no test coverage at all before this file.

That gap was real: _redact() had two live bugs found while writing these
tests. (1) The sk- pattern wrapped its entire match in a capturing group,
so the "keep the label, redact the value" substitution reproduced the
WHOLE secret verbatim and just appended "[REDACTED]" after it -- the log
line looked redacted at a glance but the raw key was still sitting right
there in the file. (2) Its character class was [A-Za-z0-9] only, which
stops matching at the first hyphen -- real Anthropic keys
("sk-ant-api03-<base64url>-<checksum>") are almost entirely hyphens/
underscores past the "sk-" prefix, so they were barely matched at all.
Both are fixed in logger.py; the tests below pin the fix and the shape of
a realistic key so this can't quietly regress.

The most important tests here don't call _redact() directly -- they write
through TaskLogger and read the actual bytes back off disk, since that's
the real guarantee this feature exists to provide (a secret must never
land in a file on disk), not just that some in-memory function returns
the right string.
"""
from logger import TaskLogger, _redact

OPENAI_STYLE_KEY = "sk-proj" + "A" * 40
ANTHROPIC_STYLE_KEY = "sk-ant-api03-" + "B" * 80 + "-" + "C" * 4


def test_bare_openai_style_key_is_fully_redacted():
    line = f"Using key {OPENAI_STYLE_KEY} for auth"
    redacted = _redact(line)
    assert OPENAI_STYLE_KEY not in redacted
    assert "[REDACTED]" in redacted


def test_bare_anthropic_style_key_with_hyphens_is_fully_redacted():
    # This is the realistic shape a real Anthropic key takes -- mostly
    # hyphens/underscores after the "sk-" prefix, not a long alnum run.
    line = f"ANTHROPIC_API_KEY={ANTHROPIC_STYLE_KEY}"
    redacted = _redact(line)
    assert ANTHROPIC_STYLE_KEY not in redacted
    assert "[REDACTED]" in redacted


def test_labeled_api_key_preserves_the_label_and_redacts_the_value():
    redacted = _redact("config: api_key: supersecret123 loaded")
    assert "supersecret123" not in redacted
    assert "api_key:" in redacted
    assert "[REDACTED]" in redacted


def test_labeled_password_is_redacted_case_insensitively():
    redacted = _redact("PASSWORD=hunter2")
    assert "hunter2" not in redacted
    assert "[REDACTED]" in redacted


def test_bearer_token_is_redacted():
    redacted = _redact("sent header Authorization: Bearer abcdef123456 to the server")
    assert "abcdef123456" not in redacted
    assert "Authorization: Bearer [REDACTED]" in redacted


def test_multiple_secrets_on_one_line_are_all_redacted():
    line = f"api_key: secret-one password: secret-two"
    redacted = _redact(line)
    assert "secret-one" not in redacted
    assert "secret-two" not in redacted


def test_ordinary_text_is_left_completely_unchanged():
    line = "Step 3: clicked the Submit button and observed a results page."
    assert _redact(line) == line


def test_task_logger_never_writes_a_secret_to_disk(tmp_path):
    logger = TaskLogger(tmp_path, f"Log in using {OPENAI_STYLE_KEY}")
    logger.action(
        1, f"Typing the key {ANTHROPIC_STYLE_KEY} into the field.",
        "type", {"index": 0, "text": OPENAI_STYLE_KEY}, "https://example.com",
    )
    logger.note(f"api_key: {OPENAI_STYLE_KEY}")
    logger.error(f"Authorization: Bearer {OPENAI_STYLE_KEY}")
    logger.finish(f"Done, used password: {OPENAI_STYLE_KEY}")

    on_disk = logger.path.read_text(encoding="utf-8")
    assert OPENAI_STYLE_KEY not in on_disk
    assert ANTHROPIC_STYLE_KEY not in on_disk
    assert on_disk.count("[REDACTED]") >= 5  # one per secret occurrence above
