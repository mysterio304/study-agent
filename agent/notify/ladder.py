"""Deadline ladder (SPEC §5): 7d, 3d, 24h and 3h before due, only while unsubmitted.

Each step is enqueued once per assignment (dedupe key `deadline:<id>:<step>`). Only the current
step is sent — an assignment first seen 2 days before its deadline gets the 3d message, not 7d
and 3d together. The 3h step is urgent and bypasses quiet hours. Buttons: «Сделал» /
«Напомни через 2 ч» / «Игнор».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.canvas.normalize import NON_SUBMITTABLE, SUBMITTED_STATES
from agent.core.clock import Clock
from agent.core.timeutil import TimeRange
from agent.db.models import Assignment, Course
from agent.notify.outbox import Urgency, enqueue
from agent.notify.templates import deadline_buttons
from agent.telegram.format import fmt_dt, h


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    before: timedelta
    urgency: Urgency


STEPS = (
    Step("3h", timedelta(hours=3), "urgent"),
    Step("24h", timedelta(hours=24), "normal"),
    Step("3d", timedelta(days=3), "normal"),
    Step("7d", timedelta(days=7), "normal"),
)


def current_step(due: datetime, now: datetime) -> Step | None:
    """The tightest step whose window we are in, or None (overdue or > 7 days away)."""
    remaining = due - now
    if remaining <= timedelta(0):
        return None
    return next((s for s in STEPS if remaining <= s.before), None)


def humanize(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, mins = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} ч" + (f" {mins} мин" if hours < 6 and mins else "")
    return f"{hours // 24} дн."


def is_open(a: Assignment) -> bool:
    online = bool(set(a.submission_types or ["online_upload"]) - NON_SUBMITTABLE)
    submitted = a.submitted_at is not None or (a.submission_state or "") in SUBMITTED_STATES
    return online and not submitted and not a.excused and a.user_state is None


def deadline_key(a: Assignment, step: str) -> str:
    """Includes the due date, so an extended deadline gets its own ladder."""
    return f"deadline:{a.id}:{int(a.due_at.timestamp())}:{step}"


def ladder_text(a: Assignment, course: Course, now: datetime, clock: Clock) -> str:
    text = (
        f"⏳ {h(course.course_code or course.name)}: «{h(a.name)}» — через "
        f"<b>{humanize(a.due_at - now)}</b> ({fmt_dt(a.due_at, clock.zone)})"
    )
    if a.html_url:
        text += f'\n<a href="{h(a.html_url)}">Открыть в Canvas</a>'
    return text


async def run_ladder(session: AsyncSession, clock: Clock, quiet: TimeRange) -> int:
    """Enqueue due ladder steps. Returns how many new messages were queued."""
    now = clock.now()
    rows = (
        await session.execute(
            select(Assignment, Course)
            .join(Course, Assignment.course_id == Course.id)
            .where(Assignment.due_at > now, Assignment.due_at <= now + STEPS[-1].before)
        )
    ).all()
    queued = 0
    for a, course in rows:
        step = current_step(a.due_at, now) if is_open(a) else None
        if step is None:
            continue
        text = ladder_text(a, course, now, clock)
        if await enqueue(
            session,
            deadline_key(a, step.name),
            text,
            urgency=step.urgency,
            clock=clock,
            quiet=quiet,
            buttons=deadline_buttons(a.id),
        ):
            queued += 1
    return queued
