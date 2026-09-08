"""
Unit tests for discord_bot.py -- its own state machine (permission gating,
the pending-confirmation routing, the single-task lock, message chunking)
had no test coverage at all before this file, despite being the interface
used to control the agent remotely. Fully offline: no real Discord
connection is made, and run_task() itself is mocked out (its own behavior
is already covered by test_agent_loop.py and friends) -- these tests are
only about discord_bot.py's own plumbing around it.

discord.Client.event() just assigns the decorated coroutine as an attribute
and returns it unchanged, so on_message/on_ready remain plain async
functions here, directly awaitable without a real Client or gateway
connection. asyncio.run() is used instead of pytest-asyncio (not a project
dependency) since these are simple, self-contained coroutines.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import discord_bot

ALLOWED_USER_ID = 12345


def _make_message(content: str, author_id: int = ALLOWED_USER_ID):
    channel = SimpleNamespace(send=AsyncMock())
    message = SimpleNamespace(author=SimpleNamespace(id=author_id), content=content, channel=channel)
    return message


@pytest.fixture(autouse=True)
def _reset_discord_bot_state(monkeypatch):
    """discord_bot.py keeps its confirmation/lock state as module globals
    (by design -- only one task is ever in flight at a time). Reset them
    around every test so tests can't leak state into each other."""
    monkeypatch.setattr(discord_bot, "_pending_confirmation", None)
    monkeypatch.setattr(discord_bot, "config", SimpleNamespace(discord_allowed_user_id=ALLOWED_USER_ID))
    assert discord_bot._task_lock.acquire(blocking=False)
    discord_bot._task_lock.release()
    yield


def test_message_from_unauthorized_user_is_silently_ignored():
    message = _make_message("do something", author_id=999)
    asyncio.run(discord_bot.on_message(message))
    message.channel.send.assert_not_called()


def test_empty_message_is_ignored():
    message = _make_message("   ")
    asyncio.run(discord_bot.on_message(message))
    message.channel.send.assert_not_called()


def test_reply_while_confirmation_pending_resolves_it_instead_of_starting_a_new_task(monkeypatch):
    run_task_spy = MagicMock()
    monkeypatch.setattr(discord_bot, "run_task", run_task_spy)

    answer_queue = discord_bot.queue.Queue()
    monkeypatch.setattr(discord_bot, "_pending_confirmation", answer_queue)

    message = _make_message("y")
    asyncio.run(discord_bot.on_message(message))

    assert answer_queue.get_nowait() is True
    assert discord_bot._pending_confirmation is None
    run_task_spy.assert_not_called()
    message.channel.send.assert_not_called()  # the reply itself gets no separate acknowledgement


@pytest.mark.parametrize("reply_text,expected", [("y", True), ("Y", True), ("yes", True), ("YES", True),
                                                  ("n", False), ("no", False), ("maybe", False), ("stop", False)])
def test_confirmation_replies_are_fail_closed(reply_text, expected, monkeypatch):
    answer_queue = discord_bot.queue.Queue()
    monkeypatch.setattr(discord_bot, "_pending_confirmation", answer_queue)

    asyncio.run(discord_bot.on_message(_make_message(reply_text)))

    assert answer_queue.get_nowait() is expected


def test_second_task_while_one_is_running_gets_told_to_wait(monkeypatch):
    run_task_spy = MagicMock()
    monkeypatch.setattr(discord_bot, "run_task", run_task_spy)
    discord_bot._task_lock.acquire()
    try:
        message = _make_message("another task")
        asyncio.run(discord_bot.on_message(message))
        message.channel.send.assert_awaited_once_with(
            "Still working on the previous task -- try again once it's done."
        )
        run_task_spy.assert_not_called()
    finally:
        discord_bot._task_lock.release()


def test_successful_task_runs_and_reports_the_result_with_its_output_path(monkeypatch):
    monkeypatch.setattr(
        discord_bot, "run_task",
        lambda task_text, config, dry_run, confirm_callback: {
            "success": True, "result": "Found the answer.", "output_path": "/tmp/out.json",
        },
    )
    message = _make_message("look something up")
    asyncio.run(discord_bot.on_message(message))

    sent_texts = [call.args[0] for call in message.channel.send.await_args_list]
    assert any("Running: look something up" in t for t in sent_texts)
    assert any("Found the answer." in t and "/tmp/out.json" in t for t in sent_texts)
    assert discord_bot._task_lock.acquire(blocking=False)  # released after the task finished
    discord_bot._task_lock.release()


def test_failed_task_also_reports_its_output_path(monkeypatch):
    # Regression: agent.py now writes a structured output record for
    # failures too (see _save_output), so the bot should surface that path
    # on failure the same way it always has on success.
    monkeypatch.setattr(
        discord_bot, "run_task",
        lambda task_text, config, dry_run, confirm_callback: {
            "success": False, "result": "Something went wrong.", "output_path": "/tmp/failed.json",
        },
    )
    message = _make_message("a task that fails")
    asyncio.run(discord_bot.on_message(message))
    sent_texts = [call.args[0] for call in message.channel.send.await_args_list]
    assert any("Task failed" in t and "Something went wrong." in t for t in sent_texts)
    assert any("/tmp/failed.json" in t for t in sent_texts)


def test_unexpected_exception_still_releases_the_lock_for_the_next_task(monkeypatch):
    def boom(task_text, config, dry_run, confirm_callback):
        raise RuntimeError("boom")

    monkeypatch.setattr(discord_bot, "run_task", boom)
    message = _make_message("this will blow up")
    asyncio.run(discord_bot.on_message(message))

    sent_texts = [call.args[0] for call in message.channel.send.await_args_list]
    assert any("Unexpected error" in t and "boom" in t for t in sent_texts)
    # The lock must be free again so a later task isn't stuck waiting forever.
    assert discord_bot._task_lock.acquire(blocking=False)
    discord_bot._task_lock.release()


def test_confirmation_flow_end_to_end_across_two_messages(monkeypatch):
    """
    Simulates the real interactive flow: a task starts, run_task() (mocked)
    calls back into confirm_callback from its worker thread, which posts a
    question and blocks -- then a second incoming message answers it.
    """
    def fake_run_task(task_text, config, dry_run, confirm_callback):
        approved = confirm_callback("Ready to do the risky thing?")
        return {
            "success": approved, "result": "Approved and done." if approved else "declined by user",
            "output_path": "/tmp/x.json",
        }

    monkeypatch.setattr(discord_bot, "run_task", fake_run_task)
    monkeypatch.setattr(discord_bot, "CONFIRMATION_TIMEOUT_S", 5)

    task_message = _make_message("do the risky thing")
    reply_message = _make_message("y")

    async def scenario():
        async def send_reply_shortly():
            await asyncio.sleep(0.05)
            await discord_bot.on_message(reply_message)

        await asyncio.gather(discord_bot.on_message(task_message), send_reply_shortly())

    asyncio.run(scenario())

    sent_texts = [call.args[0] for call in task_message.channel.send.await_args_list]
    assert any("Ready to do the risky thing?" in t for t in sent_texts)
    assert any("Approved and done." in t for t in sent_texts)


def test_confirmation_times_out_and_auto_declines(monkeypatch):
    def fake_run_task(task_text, config, dry_run, confirm_callback):
        approved = confirm_callback("Ready to do the risky thing?")
        return {"success": approved, "result": "declined by timeout" if not approved else "ok", "output_path": None}

    monkeypatch.setattr(discord_bot, "run_task", fake_run_task)
    monkeypatch.setattr(discord_bot, "CONFIRMATION_TIMEOUT_S", 0.05)

    message = _make_message("do the risky thing")
    asyncio.run(discord_bot.on_message(message))

    sent_texts = [call.args[0] for call in message.channel.send.await_args_list]
    assert any("declining automatically" in t for t in sent_texts)
    assert any("declined by timeout" in t for t in sent_texts)


def test_send_long_splits_messages_over_the_discord_limit():
    channel = SimpleNamespace(send=AsyncMock())
    short_text = "x" * 500
    long_text = "y" * 4500

    asyncio.run(discord_bot._send_long(channel, short_text))
    assert channel.send.await_count == 1

    channel.send.reset_mock()
    asyncio.run(discord_bot._send_long(channel, long_text))
    assert channel.send.await_count == 3  # 4500 chars / 1900-char chunks
    sent = "".join(call.args[0] for call in channel.send.await_args_list)
    assert sent == long_text


def test_validate_discord_config_flags_missing_token_and_user_id():
    from config import Config

    cfg = Config(llm_provider="mock", discord_bot_token="", discord_allowed_user_id=0)
    problems = discord_bot._validate_discord_config(cfg)

    assert any("DISCORD_BOT_TOKEN" in p for p in problems)
    assert any("DISCORD_ALLOWED_USER_ID" in p for p in problems)


def test_validate_discord_config_passes_with_token_and_user_id_set():
    from config import Config

    cfg = Config(llm_provider="mock", discord_bot_token="fake-token", discord_allowed_user_id=42)
    assert discord_bot._validate_discord_config(cfg) == []
