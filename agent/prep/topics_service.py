"""Topic review and self-rating flow used by the /start wizard.

Status lifecycle: draft (discovered, awaiting review) → rating (accepted, awaiting self-rating)
→ active (used by the planner). Also: dropped, merged.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Exam, Topic


async def topics_with_status(session: AsyncSession, exam_id: int, status: str) -> list[Topic]:
    q = select(Topic).where(Topic.exam_id == exam_id, Topic.status == status).order_by(Topic.id)
    return list((await session.scalars(q)).all())


async def accept_topics(session: AsyncSession, exam_id: int) -> int:
    drafts = await topics_with_status(session, exam_id, "draft")
    for t in drafts:
        t.status = "rating"
    await session.flush()
    return len(drafts)


async def next_to_rate(session: AsyncSession) -> Topic | None:
    q = (
        select(Topic)
        .join(Exam, Topic.exam_id == Exam.id)
        .where(Topic.status == "rating")
        .order_by(Exam.starts_at, Topic.id)
    )
    return await session.scalar(q)


async def rate_topic(session: AsyncSession, topic_id: int, level: int) -> Topic:
    if not 0 <= level <= 3:
        raise ValueError("level must be 0..3")
    topic = await session.get(Topic, topic_id)
    if topic is None:
        raise ValueError("unknown topic")
    if topic.status != "rating":  # replayed/double-tapped button: never reset a rated topic
        raise ValueError("topic already rated")
    topic.level, topic.progress_min, topic.status = level, 0, "active"
    await session.flush()
    return topic


async def rate_rest_of_exam(session: AsyncSession, exam_id: int, level: int) -> int:
    rest = await topics_with_status(session, exam_id, "rating")
    for t in rest:
        t.level, t.status = level, "active"
    await session.flush()
    return len(rest)


async def setup_complete(session: AsyncSession) -> bool:
    """True when every exam has active topics and nothing is waiting for review/rating."""
    exams = (await session.scalars(select(Exam))).all()
    for e in exams:
        statuses = set(
            (await session.scalars(select(Topic.status).where(Topic.exam_id == e.id))).all()
        )
        if "active" not in statuses or statuses & {"draft", "rating"}:
            return False
    return bool(exams)
