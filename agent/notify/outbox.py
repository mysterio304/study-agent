"""Quiet-hours outbox (SPEC §5).

Every non-interactive notification goes through `enqueue()`:
- `dedupe_key` is UNIQUE, so a notification can never be sent twice (restarts, re-syncs);
- `urgent` messages are sent immediately, even in quiet hours (3h deadline step, reminders);
- `normal` messages arriving in quiet hours wait until quiet hours end;
- `digest` items are never sent alone — the morning digest collects them.
`flush()` runs every minute; when many plain messages are due at once (e.g. 08:00 after a quiet
night) they are combined into one message instead of a flood.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Literal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.core.clock import Clock
from agent.core.timeutil import TimeRange, local_to_utc
from agent.db.models import OutboxMessage
from agent.telegram.notifier import Notifier

log = logging.getLogger(__name__)

Urgency = Literal["urgent", "normal", "digest"]
Buttons = list[list[tuple[str, str]]]  # rows of (text, callback_data)
COMBINE_THRESHOLD = 3
MAX_ATTEMPTS = 5


def quiet_end(now: datetime, quiet: TimeRange, clock: Clock) -> datetime | None:
    """If `now` is in quiet hours, the UTC instant they end; else None."""
    local = now.astimezone(clock.zone)
    if not quiet.contains(local.time()):
        return None
    day = local.date()
    if quiet.wraps_midnight and local.time() >= quiet.start:
        day += timedelta(days=1)
    return local_to_utc(day, quiet.end, clock.zone)


async def enqueue(
    session: AsyncSession,
    key: str,
    text: str,
    *,
    urgency: Urgency,
    clock: Clock,
    quiet: TimeRange,
    buttons: Buttons | None = None,
) -> bool:
    """Queue a message once. Returns False if `key` was already queued."""
    if await session.scalar(select(OutboxMessage.id).where(OutboxMessage.dedupe_key == key)):
        return False
    now = clock.now()
    not_before = now if urgency == "urgent" else (quiet_end(now, quiet, clock) or now)
    session.add(
        OutboxMessage(
            dedupe_key=key[:128],
            urgency=urgency,
            text=text,
            buttons_json=buttons,
            not_before=not_before,
            status="pending",
            attempts=0,
            created_at=now,
        )
    )
    await session.flush()
    return True


def keyboard(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in buttons
        ]
    )


Refresh = Callable[[AsyncSession, OutboxMessage, Clock], Awaitable[str | None]]


async def flush(
    sessionmaker: async_sessionmaker,
    notifier: Notifier,
    clock: Clock,
    *,
    refresh: Refresh | None = None,
) -> int:
    """Send due urgent/normal messages. `refresh` may rewrite a message's text right before
    sending or cancel it (returns None). Returns the number of Telegram messages sent."""
    now = clock.now()
    async with sessionmaker() as s:
        due = list(
            (
                await s.scalars(
                    select(OutboxMessage)
                    .where(
                        OutboxMessage.status == "pending",
                        OutboxMessage.urgency != "digest",
                        OutboxMessage.not_before <= now,
                    )
                    .order_by(OutboxMessage.id)
                )
            ).all()
        )
        if refresh is not None:
            kept = []
            for m in due:
                text = await refresh(s, m, clock)
                if text is None:
                    m.status = "cancelled"
                else:
                    m.text = text
                    kept.append(m)
            await s.commit()
            due = kept
    if not due:
        return 0
    plain = [m for m in due if not m.buttons_json]
    interactive = [m for m in due if m.buttons_json]
    batches: list[tuple[list[OutboxMessage], str, InlineKeyboardMarkup | None]] = [
        (m_list, "\n\n".join(m.text for m in m_list), None)
        for m_list in ([plain] if len(plain) > COMBINE_THRESHOLD else [[m] for m in plain])
    ] + [([m], m.text, keyboard(m.buttons_json)) for m in interactive]
    sent = 0
    for items, text, kb in batches:
        msg_id = await notifier.send(text, kb)
        await _mark(sessionmaker, [m.id for m in items], msg_id, now)
        sent += msg_id is not None
    return sent


async def _mark(
    sessionmaker: async_sessionmaker, ids: list[int], msg_id: int | None, now: datetime
) -> None:
    async with sessionmaker() as s:
        for row_id in ids:
            row = await s.get(OutboxMessage, row_id)
            row.attempts += 1
            if msg_id is not None or row.attempts >= MAX_ATTEMPTS:
                row.status = "sent" if msg_id is not None else "failed"
                row.sent_at, row.tg_message_id = now, msg_id
        await s.commit()


async def pending_digest(session: AsyncSession) -> list[OutboxMessage]:
    """Digest items waiting for the morning digest (not yet marked sent)."""
    return list(
        (
            await session.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.status == "pending", OutboxMessage.urgency == "digest")
                .order_by(OutboxMessage.id)
            )
        ).all()
    )


async def mark_digest_sent(sessionmaker: async_sessionmaker, ids: list[int], clock: Clock) -> None:
    await _mark(sessionmaker, ids, 0, clock.now())
