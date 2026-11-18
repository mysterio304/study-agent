"""DB-facing course mapping operations used by the /start wizard and the CLI."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.canvas.api import CanvasCourse
from agent.db.models import Course, Exam
from agent.prep.course_mapping import MappingSuggestion, suggest_mapping


def _to_canvas_course(row: Course) -> CanvasCourse:
    return CanvasCourse(
        id=row.id,
        name=row.name,
        course_code=row.course_code,
        current_score=row.current_score,
        current_grade=row.current_grade,
        apply_group_weights=row.apply_group_weights,
    )


async def list_courses(session: AsyncSession) -> list[Course]:
    return list((await session.scalars(select(Course).order_by(Course.name))).all())


async def exams_in_order(session: AsyncSession) -> list[Exam]:
    return list((await session.scalars(select(Exam).order_by(Exam.starts_at))).all())


async def suggestions_for_unconfirmed(
    session: AsyncSession,
) -> list[tuple[Exam, MappingSuggestion]]:
    exams = [e for e in await exams_in_order(session) if not e.mapping_confirmed]
    confirmed_ids = {
        e.course_id for e in await exams_in_order(session) if e.mapping_confirmed and e.course_id
    }
    courses = [
        _to_canvas_course(c) for c in await list_courses(session) if c.id not in confirmed_ids
    ]
    suggestions = suggest_mapping([e.title for e in exams], courses)
    return list(zip(exams, suggestions, strict=True))


async def confirm_mapping(session: AsyncSession, exam_id: int, course_id: int | None) -> Exam:
    exam = await session.get(Exam, exam_id)
    if exam is None:
        raise ValueError(f"Unknown exam {exam_id}")
    if course_id is not None and await session.get(Course, course_id) is None:
        raise ValueError(f"Unknown course {course_id}")
    exam.course_id = course_id
    exam.mapping_confirmed = True
    await session.flush()
    return exam
