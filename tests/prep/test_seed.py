from datetime import UTC, datetime

from sqlalchemy import select

from agent.db.models import Exam
from agent.prep.seed import FINALS, seed_exams


async def test_seed_creates_four_exams_in_utc(sessionmaker) -> None:
    async with sessionmaker() as s:
        created = await seed_exams(s)
        await s.commit()
    assert created == 4
    async with sessionmaker() as s:
        exams = (await s.scalars(select(Exam).order_by(Exam.starts_at))).all()
    assert [e.title for e in exams] == [f.title for f in FINALS]
    networks = exams[0]
    # 2026-12-08 09:30 HKT == 01:30 UTC
    assert networks.starts_at == datetime(2026, 12, 8, 1, 30, tzinfo=UTC)
    assert networks.duration_min == 120
    assert networks.final_weight == 0.7
    assert networks.weight_source == "user"
    assert all(e.target_grade == "A-" for e in exams)
    assert exams[1].final_weight is None and exams[1].weight_source == "unknown"


async def test_seed_is_idempotent_and_keeps_user_edits(sessionmaker) -> None:
    async with sessionmaker() as s:
        await seed_exams(s)
        await s.commit()
    async with sessionmaker() as s:
        algebra = await s.scalar(select(Exam).where(Exam.title == "Algebra"))
        algebra.target_grade = "A"
        await s.commit()
    async with sessionmaker() as s:
        assert await seed_exams(s) == 0
        await s.commit()
    async with sessionmaker() as s:
        assert (await s.scalar(select(Exam).where(Exam.title == "Algebra"))).target_grade == "A"
        assert len((await s.scalars(select(Exam))).all()) == 4
