"""
runtime.py — Bridge between the web server threads and the bot's asyncio event loop.

The portal's HTTP server runs in background threads, while the bot (and its Telegram
connection) lives on the asyncio loop started by python-telegram-bot. Web actions such as
SOS or incident reports are coroutines that must run on that loop so alerts go out through
the same bot. `attach()` is called once at start-up; `run()` executes a coroutine there.
"""

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_bot = None


class NullBot:
    """Stand-in used when the portal runs without the bot (e.g. tests): drops outgoing messages."""
    username = os.getenv("BOT_USERNAME", "TravelBuddyBot")

    async def send_message(self, *args, **kwargs):
        return None

    async def send_location(self, *args, **kwargs):
        return None

    async def send_photo(self, *args, **kwargs):
        return None

    async def send_voice(self, *args, **kwargs):
        return None


def attach(loop: asyncio.AbstractEventLoop, bot) -> None:
    global _loop, _bot
    _loop, _bot = loop, bot
    logger.info("Web portal attached to the bot runtime (@%s).", getattr(bot, "username", "?"))


def bot():
    return _bot or NullBot()


def is_attached() -> bool:
    """True while the Telegram bot's event loop is running (used by the /healthz probe)."""
    return _loop is not None and _loop.is_running() and _bot is not None


def bot_username() -> str:
    return getattr(bot(), "username", None) or NullBot.username


def run(coro, timeout: float = 60.0):
    """Run a coroutine on the bot loop (if attached and running), else on a private loop."""
    if _loop is not None and _loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)
    return asyncio.run(coro)


def run_background(coro) -> None:
    """Fire-and-forget a coroutine on the bot loop (e.g. warm a cache). No-op without a running loop."""
    if _loop is not None and _loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, _loop)
    else:
        coro.close()
