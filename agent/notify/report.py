"""Weekly report and streaks (SPEC §6 Enforcement) — template only, no LLM."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.brain.accounting import local_day_start
from agent.config import Settings
from agent.core.clock import Clock
from agent.db.models import Block, Checkin, Exam, LlmUsage, StudySession, Topic
from agent.planner.blocks import debt
from agent.planner.render import short_exam
from agent.telegram.format import h


async def study_days(session: AsyncSession, clock: Clock, since: date) -> set[date]:
    start = local_day_start(clock) - timedelta(days=(clock.today() - since).days)
    rows = (
        await session.scalars(
            select(StudySession.created_at).where(StudySession.created_at >= start)
        )
    ).all()
    return {r.astimezone(clock.zone).date() for r in rows}


def streak(days: set[date], today: date) -> int:
    """Consecutive study days ending today (or yesterday, if today has nothing yet)."""
    d = today if today in days else today - timedelta(days=1)
    n = 0
    while d in days:
        n += 1
        d -= timedelta(days=1)
    return n


async def weekly_report(session: AsyncSession, settings: Settings, clock: Clock) -> str:
    today = clock.today()
    week_start = local_day_start(clock) - timedelta(days=6)
    blocks = (
        await session.scalars(
            select(Block).where(
                Block.starts_at >= week_start,
                Block.starts_at < clock.now(),
                Block.status != "moved",
            )
        )
    ).all()
    done = sum(b.status == "done" for b in blocks)
    sessions = (
        await session.scalars(select(StudySession).where(StudySession.created_at >= week_start))
    ).all()
    topic_exam = {t.id: t.exam_id for t in (await session.scalars(select(Topic))).all()}
    exams = {e.id: short_exam(e.title) for e in (await session.scalars(select(Exam))).all()}
    minutes_by_exam: dict[int, int] = defaultdict(int)
    for s in sessions:
        if s.topic_id in topic_exam:
            minutes_by_exam[topic_exam[s.topic_id]] += s.minutes
    total_h = sum(s.minutes for s in sessions) / 60
    checkins = await session.scalar(
        select(func.count())
        .select_from(Checkin)
        .where(Checkin.status == "done", Checkin.date_local >= today - timedelta(days=6))
    )
    spend = await session.scalar(
        select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0)).where(LlmUsage.at >= week_start)
    )
    d = await debt(session, clock)
    days = await study_days(session, clock, today - timedelta(days=60))
    rate = f"{100 * done / len(blocks):.0f}%" if blocks else "—"
    lines = [
        "📈 <b>Итоги недели</b>",
        f"Блоки: {done} из {len(blocks)} ({rate}) · учёба {total_h:.1f} ч · серия {streak(days, today)} дн.",
        f"Check-in: {checkins}/7 · долг {d.blocks} блоков ({d.hours} ч) · LLM ${float(spend):.2f}",
    ]
    if minutes_by_exam:
        lines.append(
            "По экзаменам: "
            + ", ".join(
                f"{h(exams.get(eid, '?'))} {m / 60:.1f} ч"
                for eid, m in sorted(minutes_by_exam.items())
            )
        )
    return "\n".join(lines)
