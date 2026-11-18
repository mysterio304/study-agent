"""Events → outbox.

`enqueue_events()` runs INSIDE the sync transaction that stored the new Canvas state, so state and
notifications commit atomically: a crash can never record a change without its notification (or
vice versa). Each event is rendered and queued in isolation — one bad event cannot drop the rest.

The only LLM use is announcement summaries, computed by `summarise()` BEFORE the transaction opens
(no DB lock across an API call), only for texts longer than SUMMARY_MIN_CHARS, and stored on the
announcement so they are never regenerated (SPEC §9a.1).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from agent.brain.budgets import PromptTooLarge, truncate_to_tokens
from agent.brain.llm import BudgetExceeded, LlmClient, text_of
from agent.core.clock import Clock
from agent.core.timeutil import TimeRange
from agent.notify.events import Event, NewAnnouncement
from agent.notify.outbox import enqueue
from agent.notify.templates import render

log = logging.getLogger(__name__)

SUMMARY_MIN_CHARS = 500
SUMMARY_SYSTEM = (
    "Summarise a university course announcement for a student, in Russian, in 1-3 short sentences. "
    "Keep dates, times, rooms, deadlines, weights and exam info exactly. Course terms stay in English. "
    "Plain text only."
)


async def summarise(llm: LlmClient | None, text: str) -> str | None:
    if llm is None or len(text) <= SUMMARY_MIN_CHARS:
        return None
    try:
        res = await llm.call(
            "announcement_summary",
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": truncate_to_tokens(text, 2_500)}],
        )
    except (BudgetExceeded, PromptTooLarge) as exc:
        log.info("Announcement not summarised: %s", type(exc).__name__)
        return None
    except Exception:  # API failure → template falls back to the trimmed original
        log.exception("Announcement summary failed")
        return None
    return text_of(res.message) or None


async def enqueue_events(
    session: AsyncSession,
    events: Sequence[Event],
    *,
    clock: Clock,
    quiet: TimeRange,
    summaries: Mapping[int, str] | None = None,
) -> int:
    """Queue one outbox message per event in the caller's transaction. Returns how many queued."""
    queued = 0
    for e in events:
        body = (summaries or {}).get(e.id) if isinstance(e, NewAnnouncement) else None
        try:
            r = render(e, clock.zone, announcement_text=body)
            async with session.begin_nested():  # a failing event rolls back only itself
                queued += await enqueue(
                    session,
                    r.key,
                    r.text,
                    urgency=r.urgency,
                    clock=clock,
                    quiet=quiet,
                    buttons=r.buttons,
                )
        except Exception:
            log.exception("Could not queue notification for %s", type(e).__name__)
    return queued
