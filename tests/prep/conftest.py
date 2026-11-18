"""Fixtures for quiz / mock / file tests: an indexed Networks course with two topics."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from agent.core.clock import FixedClock
from agent.db.models import Course, CourseFile, Exam, Topic
from agent.files.indexer import index_local_file
from agent.prep.seed import seed_exams
from tests.files.samples import make_pdf
from tests.planner.conftest import HKT, at


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(at(date(2026, 11, 19), 10), HKT)


@pytest.fixture
async def db(sessionmaker, clk, tmp_path):
    async with sessionmaker() as s:
        await seed_exams(s)
        s.add(
            Course(
                id=101,
                name="CS3201",
                apply_group_weights=True,
                content_hash="x",
                synced_at=clk.now(),
            )
        )
        await s.flush()
        (await s.scalar(select(Exam).where(Exam.title == "Computer Networks"))).course_id = 101
        s.add_all(
            [
                Topic(
                    id=1,
                    exam_id=1,
                    name="TCP congestion control",
                    effort_hours=3,
                    importance=2,
                    level=0,
                    target_level=2,
                    status="active",
                ),
                Topic(
                    id=2,
                    exam_id=1,
                    name="Routing algorithms",
                    effort_hours=3,
                    importance=2,
                    level=1,
                    target_level=2,
                    status="active",
                ),
            ]
        )
        f = CourseFile(
            id=1,
            course_id=101,
            display_name="L5.pdf",
            source="files_api",
            kind="lecture",
            index_state="pending",
        )
        s.add(f)
        await s.commit()
    pdf = make_pdf(
        tmp_path / "L5.pdf",
        [
            "TCP congestion control: cwnd and slow start",
            "Routing algorithms: Dijkstra and Bellman-Ford",
        ],
    )
    await index_local_file(sessionmaker, 1, pdf, clk)
    return sessionmaker
