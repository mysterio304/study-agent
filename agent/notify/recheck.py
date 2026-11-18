"""Send-time recheck for queued deadline-related messages.

A ladder step queued during quiet hours may wait until morning. Before it is sent we reload the
assignment: if the work was submitted, marked «Сделал»/«Игнор», or the deadline already passed,
the message is cancelled; otherwise the countdown text is recomputed so it is never stale.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from agent.core.clock import Clock
from agent.db.models import Assignment, Course, OutboxMessage
from agent.notify.ladder import is_open, ladder_text


async def refresh_message(session: AsyncSession, msg: OutboxMessage, clock: Clock) -> str | None:
    """Return the text to send now, or None to cancel the message."""
    kind, _, rest = msg.dedupe_key.partition(":")
    if kind not in ("deadline", "missing"):
        return msg.text
    assignment = await session.get(Assignment, int(rest.split(":")[0]))
    if assignment is None:
        return None
    if kind == "missing":
        submitted = not is_open(assignment) and assignment.user_state is None
        return None if assignment.user_state or submitted else msg.text
    now = clock.now()
    if not is_open(assignment) or assignment.due_at is None or assignment.due_at <= now:
        return None
    course = await session.get(Course, assignment.course_id)
    return ladder_text(assignment, course, now, clock) if course else msg.text
