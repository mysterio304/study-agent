"""Grade views on synced data + exam-info and busy-time tools."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from agent.brain.tool_handlers import ToolContext
from agent.brain.tools import run_tool
from agent.config import Settings
from agent.core.clock import FixedClock
from agent.db.models import Assignment, AssignmentGroup, BusySlot, Course, Exam, Proposal, Topic
from agent.grades.service import deadlines_view, grades_view, need_for_exam, render_need
from agent.prep.seed import seed_exams
from tests.planner.conftest import HKT, at

SETTINGS = Settings(_env_file=None)


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(at(date(2026, 11, 19), 10), HKT)


@pytest.fixture
async def db(sessionmaker, clk):
    async with sessionmaker() as s:
        await seed_exams(s)
        s.add(
            Course(
                id=101,
                name="CS3201 Computer Networks",
                course_code="CS3201",
                current_score=80.0,
                current_grade="A-",
                apply_group_weights=True,
                content_hash="x",
                synced_at=clk.now(),
            )
        )
        await s.flush()
        s.add_all(
            [
                AssignmentGroup(
                    id=1, course_id=101, name="Labs", group_weight=30, content_hash="x"
                ),
                AssignmentGroup(
                    id=2, course_id=101, name="Final Exam", group_weight=70, content_hash="x"
                ),
            ]
        )
        await s.flush()
        s.add_all(
            [
                Assignment(
                    id=1,
                    course_id=101,
                    group_id=1,
                    name="Lab 1",
                    points_possible=10,
                    score=9,
                    due_at=clk.now() - timedelta(days=5),
                    submitted_at=clk.now() - timedelta(days=6),
                    content_hash="x",
                    synced_at=clk.now(),
                ),
                Assignment(
                    id=2,
                    course_id=101,
                    group_id=1,
                    name="Lab 2",
                    points_possible=10,
                    due_at=clk.now() + timedelta(days=2),
                    submission_types=["online_upload"],
                    content_hash="x",
                    synced_at=clk.now(),
                ),
                Assignment(
                    id=3,
                    course_id=101,
                    group_id=2,
                    name="Final exam",
                    points_possible=100,
                    due_at=None,
                    content_hash="x",
                    synced_at=clk.now(),
                ),
            ]
        )
        exam = await s.scalar(select(Exam).where(Exam.title == "Computer Networks"))
        exam.course_id = 101
        for eid in (1, 2, 3, 4):
            s.add(
                Topic(
                    exam_id=eid,
                    name=f"T{eid}",
                    effort_hours=2,
                    importance=2,
                    level=1,
                    target_level=2,
                    status="active",
                )
            )
        await s.commit()
    return sessionmaker


async def test_need_uses_canvas_groups(db) -> None:
    async with db() as s:
        exam = await s.scalar(select(Exam).where(Exam.title == "Computer Networks"))
        r = await need_for_exam(s, exam)
    assert r.weight_source == "canvas" and r.final_weight == pytest.approx(0.7)
    assert r.coursework_avg == pytest.approx(90.0) and r.coursework_zero == pytest.approx(45.0)
    text = render_need(exam, r, ["A-"])
    assert "A- (≥80%)" in text and "если несданное = 0" in text and "приблизительная" in text


async def test_need_with_unknown_weight(db) -> None:
    async with db() as s:
        exam = await s.scalar(select(Exam).where(Exam.title == "Algebra"))
        assert "вес final неизвестен" in render_need(exam, await need_for_exam(s, exam), ["A"])


async def test_deadlines_and_grades_views(db, clk) -> None:
    async with db() as s:
        dl = await deadlines_view(s, clk)
        gr = await grades_view(s, clk)
    assert (
        "⏳" in dl and "Lab 2" in dl and "Lab 1" not in dl
    )  # Lab 1 is older than the 3-day lookback
    assert "Networks: 80.0% (A-)" in gr and "Lab 1 — 9/10" in gr


async def test_set_exam_info_tool(db, clk) -> None:
    ctx = ToolContext(db, SETTINGS, clk)
    text, err = await run_tool(
        ctx, "grades", "set_exam_info", {"exam": "Algebra", "final_weight": 60}
    )
    assert not err and "60%" in text
    _, err = await run_tool(
        ctx, "grades", "set_exam_info", {"exam": "Algebra", "final_weight": 160}
    )
    assert err
    async with db() as s:
        algebra = await s.scalar(select(Exam).where(Exam.title == "Algebra"))
        assert (algebra.final_weight, algebra.weight_source) == (0.6, "user")


async def test_busy_time_tool_adds_slot_and_proposes_replan(db, clk) -> None:
    ctx = ToolContext(db, SETTINGS, clk)
    text, err = await run_tool(
        ctx,
        "chat",
        "add_busy_time",
        {
            "weekday": 4,
            "start": "10:00",
            "end": "12:50",
            "label": "Лекция CS3201",
            "until": "2026-11-21",
        },
    )
    assert not err and ctx.proposals, text
    async with db() as s:
        slot = await s.scalar(select(BusySlot))
        assert (slot.weekday, slot.start_local, slot.valid_until) == (
            4,
            "10:00",
            date(2026, 11, 21),
        )
        p = await s.get(Proposal, ctx.proposals[0])
        assert p.status == "pending"
    for bad in (
        {"start": "12:00", "end": "10:00", "weekday": 1},
        {"start": "10:00", "end": "11:00"},
        {"start": "10:00", "end": "11:00", "weekday": 9},
    ):
        _, err = await run_tool(ctx, "chat", "add_busy_time", bad)
        assert err
