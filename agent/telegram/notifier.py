"""Outbound messages to the owner. Telegram in production, console in dry-run demos.

(The quiet-hours outbox with dedupe keys arrives in M3; M2 messages are only sent inside the
study window or in direct reply to the user.)
"""

from __future__ import annotations

import html
import logging
import re
import sys
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4000


class Notifier(Protocol):
    async def send(self, text: str, kb: InlineKeyboardMarkup | None = None) -> int | None: ...
    async def send_voice(self, audio: bytes, caption: str | None = None) -> int | None: ...


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries so HTML tags inside a line are never cut."""
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            parts.append(current)
            current = ""
        current += ("\n" if current else "") + line[:limit]
    return [*parts, current] if current else parts


class TelegramNotifier:
    def __init__(self, bot: Bot, owner_id: int) -> None:
        self._bot, self._owner = bot, owner_id

    async def send(self, text: str, kb: InlineKeyboardMarkup | None = None) -> int | None:
        chunks = split_message(text)
        msg_id = None
        for i, chunk in enumerate(chunks):
            try:
                msg = await self._bot.send_message(
                    self._owner, chunk, reply_markup=kb if i == len(chunks) - 1 else None
                )
                msg_id = msg.message_id
            except TelegramAPIError:
                log.exception("Failed to send Telegram message")
                return None
        return msg_id

    async def send_voice(self, audio: bytes, caption: str | None = None) -> int | None:
        try:
            msg = await self._bot.send_voice(
                self._owner, BufferedInputFile(audio, filename="voice.ogg"), caption=caption
            )
        except TelegramAPIError:
            log.exception("Failed to send voice message")
            return None
        return msg.message_id


class ConsoleNotifier:
    """Prints messages (HTML tags stripped) — used by dry-run demos."""

    async def send(self, text: str, kb: InlineKeyboardMarkup | None = None) -> int | None:
        plain = html.unescape(re.sub(r"</?(b|i|code)>", "", text))
        buttons = ""
        if kb is not None:
            buttons = "\n  " + "  ".join(f"[{b.text}]" for row in kb.inline_keyboard for b in row)
        sys.stdout.write(f"\n── bot ──\n{plain}{buttons}\n")
        return 0  # "delivered" (callers treat None as a failed send)

    async def send_voice(self, audio: bytes, caption: str | None = None) -> int | None:
        sys.stdout.write(f"\n── bot (🎙 voice, {len(audio)} bytes) ──\n{caption or ''}\n")
        return 0
