from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from agent.canvas.fake import FakeCanvasSource
from agent.canvas.sync import sync_courses
from agent.db.models import Exam
from agent.prep.mapping_service import confirm_mapping, suggestions_for_unconfirmed
from agent.prep.seed import seed_exams


@pytest.fixture
async def seeded(sessionmaker, clock):
    async with sessionmaker() as s:
        await seed_exams(s)
        await sync_courses(s, FakeCanvasSource(), clock)
        await s.commit()
    return sessionmaker


async def test_confirming_removes_exam_and_course_from_suggestions(seeded) -> None:
    async with seeded() as s:
        pending = await suggestions_for_unconfirmed(s)
        assert len(pending) == 4
        networks, suggestion = pending[0]
        await confirm_mapping(s, networks.id, suggestion.course_id)
        await s.commit()
    async with seeded() as s:
        pending = await suggestions_for_unconfirmed(s)
        assert next(e.title for e, _ in pending) == "Design and Analysis of Algorithms"
        assert all(sug.course_id != 101 for _, sug in pending)
        exam = await s.scalar(select(Exam).where(Exam.title == "Computer Networks"))
        assert exam.course_id == 101 and exam.mapping_confirmed


async def test_confirm_without_course_and_unknown_ids(seeded) -> None:
    async with seeded() as s:
        exam = await s.scalar(select(Exam).where(Exam.title == "Algebra"))
        result = await confirm_mapping(s, exam.id, None)
        assert result.course_id is None and result.mapping_confirmed
        with pytest.raises(ValueError):
            await confirm_mapping(s, 9999, None)
        with pytest.raises(ValueError):
            await confirm_mapping(s, exam.id, 424242)


async def test_utc_column_round_trip(seeded) -> None:
    async with seeded() as s:
        exam = await s.scalar(select(Exam).where(Exam.title == "Algebra"))
        assert exam.starts_at.tzinfo is UTC
        exam.starts_at = datetime(2026, 12, 12, 10, 30, tzinfo=UTC)
        await s.commit()
    async with seeded() as s:
        exam = await s.scalar(select(Exam).where(Exam.title == "Algebra"))
        assert exam.starts_at == datetime(2026, 12, 12, 10, 30, tzinfo=UTC)
