"""Topic-level and topic-importance changes as proposals (applied only via button).

- Diagnostic: per topic 2 quiz questions → 2/2 suggests level ≥ 2, 0/2 suggests level ≤ 1.
- Past papers: how often a topic appears in indexed past papers → importance 1–3
  (SPEC §6 Priority: "topic importance — frequency in past papers").
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.core.clock import Clock
from agent.db.models import CourseFile, Exam, Proposal, Quiz, Topic
from agent.files.search import FtsRetriever
from agent.planner.render import short_exam

PROPOSAL_TTL = timedelta(days=3)
FIELDS = {"level", "importance"}
MIN_PAPERS_TO_DOWNGRADE = 2


def suggested_level(level: int, correct: int, total: int) -> int:
    if total and correct == total:
        return max(level, 2)
    if correct == 0:
        return min(level, 1)
    return level


def suggested_importance(old: int, hits: int, top_cut: int, papers: int) -> int:
    """Conservative: frequent topics → 3; never-mentioned topics lose one star, and only once at
    least MIN_PAPERS_TO_DOWNGRADE past papers are indexed; one paper is too little evidence."""
    if hits > 0 and top_cut > 0 and hits >= top_cut:
        return 3
    if hits > 0:
        return max(old, 2)
    if papers >= MIN_PAPERS_TO_DOWNGRADE:
        return max(1, old - 1)
    return old


async def diagnostic_changes(session: AsyncSession, quiz_ids: list[int]) -> list[dict[str, Any]]:
    changes = []
    for quiz in (await session.scalars(select(Quiz).where(Quiz.id.in_(quiz_ids)))).all():
        topic = await session.get(Topic, quiz.topic_id)
        answered = [q for q in quiz.questions_json if q.get("answer") is not None]
        if topic is None or not answered:
            continue
        correct = sum(q["answer"] == q["correct"] for q in answered)
        new = suggested_level(topic.level, correct, len(answered))
        if new != topic.level:
            changes.append(
                {
                    "topic_id": topic.id,
                    "field": "level",
                    "old": topic.level,
                    "value": new,
                    "label": f"{topic.name}: {topic.level} → {new} ({correct}/{len(answered)})",
                }
            )
    return changes


async def importance_changes(session: AsyncSession, exam_id: int) -> list[dict[str, Any]]:
    exam = await session.get(Exam, exam_id)
    if exam is None or exam.course_id is None:
        return []
    papers = await session.scalar(
        select(func.count())
        .select_from(CourseFile)
        .where(
            CourseFile.course_id == exam.course_id,
            CourseFile.kind == "past_paper",
            CourseFile.index_state == "indexed",
        )
    )
    if not papers:
        return []
    topics = (
        await session.scalars(
            select(Topic).where(Topic.exam_id == exam_id, Topic.status == "active")
        )
    ).all()
    retriever = FtsRetriever()
    hits = {
        t.id: await retriever.count_hits(
            session, t.name, course_id=exam.course_id, kinds=("past_paper",)
        )
        for t in topics
    }
    ranked = sorted((h for h in hits.values() if h > 0), reverse=True)
    top_cut = ranked[max(0, len(ranked) // 3 - 1)] if ranked else 0
    changes = []
    for t in topics:
        new = suggested_importance(t.importance, hits[t.id], top_cut, int(papers))
        if new != t.importance:
            changes.append(
                {
                    "topic_id": t.id,
                    "field": "importance",
                    "old": t.importance,
                    "value": new,
                    "label": f"{t.name}: {'★' * t.importance} → {'★' * new} ({hits[t.id]} упоминаний)",
                }
            )
    return changes


async def create_topic_proposal(
    session: AsyncSession, kind: str, summary: str, changes: list[dict[str, Any]], clock: Clock
) -> Proposal | None:
    if not changes:
        return None
    now = clock.now()
    p = Proposal(
        created_at=now,
        kind=kind,
        summary_ru=summary,
        status="pending",
        expires_at=now + PROPOSAL_TTL,
        options_json=[{"key": "A", "changes": changes}, {"key": "KEEP"}],
    )
    session.add(p)
    await session.flush()
    return p


async def apply_topic_changes(
    session: AsyncSession, changes: list[dict[str, Any]], clock: Clock
) -> int:
    applied = 0
    for c in changes:
        topic = await session.get(Topic, c["topic_id"])
        if topic is None or c["field"] not in FIELDS:
            continue
        value = int(c["value"])
        if c["field"] == "level" and 0 <= value <= 3:
            topic.level, topic.progress_min, topic.level_changed_at = value, 0, clock.now()
            applied += 1
        elif c["field"] == "importance" and 1 <= value <= 3:
            topic.importance = value
            applied += 1
    await session.flush()
    return applied


def proposal_title(kind: str, exam: Exam | None) -> str:
    name = short_exam(exam.title) if exam else ""
    return {
        "topic_levels": f"Диагностика {name}: уровни тем",
        "topic_importance": f"Past papers {name}: важность тем",
    }.get(kind, "Изменение тем")
