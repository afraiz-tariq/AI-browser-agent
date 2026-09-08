#!/usr/bin/env python3
"""
Discord bot interface for the AI browser agent.

    python discord_bot.py

Lets you DM the bot (or message it in a server it's in) a task from
anywhere with the Discord app and internet -- no port forwarding, no
firewall rules, no VPN, nothing exposed on this PC. The bot makes an
outbound connection to Discord like any other Discord bot; nothing listens
for inbound connections.

One-time setup (see README's "Discord bot interface" section for the full
walkthrough): create an app at https://discord.com/developers/applications,
grab its bot token into DISCORD_BOT_TOKEN, enable the "Message Content
Intent" toggle on the Bot tab (bots can't read message text without it),
and set DISCORD_ALLOWED_USER_ID to your own Discord user ID so the bot only
ever obeys you.

Unlike server.py's HTTP approach (superseded by this bot -- see git
history), Discord's back-and-forth messaging lets sensitive-action
confirmations be genuinely interactive: the bot posts "Ready to click
Submit, y/n?" into the same channel and blocks the task's worker thread on
a queue.Queue().get(timeout=CONFIRMATION_TIMEOUT_S) for your reply, instead
of having to auto-decline every sensitive action the way a one-shot HTTP
request would. See run_task()'s confirm_callback parameter in agent.py.
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading
from dataclasses import replace
from typing import Callable

import discord

from agent import run_task
from config import load_config

# Bot-triggered tasks always run headless: nobody is watching this PC's
# screen remotely, and offer_manual_resolution() in agent.py already skips
# straight to a clean "manual login required" failure when config.headless
# is True (rather than calling input(), which would hang the worker thread
# forever with no terminal reply possible). Forcing this here means a
# HEADLESS=false left over from CLI use can't silently reintroduce that
# hang -- see agent.py's offer_manual_resolution() docstring.
config = replace(load_config(), headless=True)

_task_lock = threading.Lock()
# Set to the queue.Queue a confirm_callback is blocking on while a
# sensitive-action question is outstanding, None otherwise. Only one task
# (and so at most one pending confirmation) is ever in flight at a time,
# guarded by _task_lock, so a single module-level slot is enough.
_pending_confirmation: queue.Queue | None = None

_DISCORD_MAX_LEN = 2000
_CHUNK_SIZE = 1900  # headroom under Discord's 2000-char cap
# How long a sensitive-action confirmation waits for a reply before
# auto-declining. A module-level constant (rather than a literal inline in
# _make_confirm_callback) so tests can monkeypatch it down from 5 minutes
# to exercise the timeout path without actually waiting 5 minutes.
CONFIRMATION_TIMEOUT_S = 300

intents = discord.Intents.default()
intents.message_content = True  # required to read message text at all
client = discord.Client(intents=intents)


async def _send_long(channel: discord.abc.Messageable, text: str) -> None:
    """Sends `text`, splitting into multiple messages if it's over Discord's 2000-char limit."""
    if len(text) <= _DISCORD_MAX_LEN:
        await channel.send(text)
        return
    for i in range(0, len(text), _CHUNK_SIZE):
        await channel.send(text[i:i + _CHUNK_SIZE])


def _make_confirm_callback(
    channel: discord.abc.Messageable, loop: asyncio.AbstractEventLoop
) -> Callable[[str], bool]:
    """
    Builds a confirm_callback (see run_task() in agent.py) for one task run.

    This runs on the executor thread, not the event loop -- run_task() calls
    it synchronously and blocks on it -- so posting the question has to be
    scheduled back onto the event loop via run_coroutine_threadsafe() rather
    than awaited directly.
    """
    def confirm(prompt: str) -> bool:
        global _pending_confirmation
        answer_queue: queue.Queue = queue.Queue()
        _pending_confirmation = answer_queue
        asyncio.run_coroutine_threadsafe(
            channel.send(f"{prompt}\nReply **y** to continue or **n** to decline "
                         f"(auto-declines in {CONFIRMATION_TIMEOUT_S // 60} minutes)."),
            loop,
        )
        try:
            return answer_queue.get(timeout=CONFIRMATION_TIMEOUT_S)
        except queue.Empty:
            _pending_confirmation = None
            asyncio.run_coroutine_threadsafe(
                channel.send("No reply within the time limit -- declining automatically."), loop,
            )
            return False

    return confirm


def _run_task_sync(task_text: str, channel: discord.abc.Messageable, loop: asyncio.AbstractEventLoop) -> dict:
    """
    The actual synchronous run_task() call, executed on a worker thread via
    loop.run_in_executor() -- run_task() drives real Playwright/openpyxl
    calls and can take 30-90+ seconds, and calling it directly from the
    async on_message handler would block the event loop and get the bot
    disconnected for missing Discord's heartbeat.
    """
    confirm_callback = _make_confirm_callback(channel, loop)
    return run_task(task_text, config, dry_run=False, confirm_callback=confirm_callback)


async def _send_result(channel: discord.abc.Messageable, outcome: dict) -> None:
    # A structured record is now saved for every run, success or failure
    # (see agent.py's _save_output) -- show its path either way, matching
    # what `python agent.py` itself prints on the CLI.
    if outcome["success"]:
        text = f"✅ Task complete:\n{outcome['result']}"
    else:
        text = f"❌ Task failed:\n{outcome['result']}"
    if outcome.get("output_path"):
        text += f"\n\nSaved to: {outcome['output_path']}"
    await _send_long(channel, text)


@client.event
async def on_ready() -> None:
    print(f"Logged in as {client.user} (id={client.user.id})")
    print(f"Only accepting tasks from Discord user ID: {config.discord_allowed_user_id}")


@client.event
async def on_message(message: discord.Message) -> None:
    global _pending_confirmation

    if message.author.id != config.discord_allowed_user_id:
        return  # not the one authorized user -- ignore silently, no reply at all

    content = message.content.strip()
    if not content:
        return

    # If a sensitive-action question is outstanding, this message is the
    # answer to it, not a new task -- anything that isn't an explicit
    # y/yes counts as a decline, same fail-closed default as a timeout.
    if _pending_confirmation is not None:
        answer_queue, _pending_confirmation = _pending_confirmation, None
        answer_queue.put(content.lower() in ("y", "yes"))
        return

    if not _task_lock.acquire(blocking=False):
        await message.channel.send("Still working on the previous task -- try again once it's done.")
        return

    try:
        await _send_long(message.channel, f"🤖 Running: {content}")
        loop = asyncio.get_running_loop()
        outcome = await loop.run_in_executor(None, _run_task_sync, content, message.channel, loop)
        await _send_result(message.channel, outcome)
    except Exception as e:
        await message.channel.send(f"❌ Unexpected error running that task: {e}")
    finally:
        _task_lock.release()


def _validate_discord_config(cfg) -> list[str]:
    """The bot's own config problems, on top of agent.py's general ones --
    split out from main() so it's testable without also having to mock
    client.run()'s real Discord connection attempt."""
    problems = cfg.validate()
    if not cfg.discord_bot_token:
        problems.append("DISCORD_BOT_TOKEN is not set. See .env.example.")
    if not cfg.discord_allowed_user_id:
        problems.append(
            "DISCORD_ALLOWED_USER_ID is not set (or is 0) -- the bot would obey no one. See .env.example."
        )
    return problems


def main() -> None:
    problems = _validate_discord_config(config)
    if problems:
        print("Configuration problem(s) found:")
        for p in problems:
            print(f"  - {p}")
        print("\nSee .env.example for the required settings.")
        sys.exit(1)

    client.run(config.discord_bot_token)


if __name__ == "__main__":
    main()
