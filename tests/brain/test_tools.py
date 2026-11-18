"""Tool handlers through the dispatcher: compact outputs, validation errors, safety limits."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from agent.brain.tool_handlers import ToolContext
from agent.brain.tools import run_tool
from agent.canvas.fake import FakeCanvasSource
from agent.db.models import (
    Agreement,
    Course,
    Exam,
    Proposal,
    Reminder,
    Setting,
    StudySession,
    Topic,
)
from agent.flows import Services, apply_choice, propose_replan
from tests.notify.test_jobs import Recorder


@pytest.fixture
def ctx(seeded, settings, clk) -> ToolContext:
    return ToolContext(seeded, settings, clk)


async def call(
    ctx: ToolContext, name: str, args: dict | None = None, profile: str = "chat"
) -> tuple[str, bool]:
    return await run_tool(ctx, profile, name, args or {})


async def test_tool_outside_profile_is_refused(ctx) -> None:
    text, err = await call(ctx, "propose_undo", profile="material")
    assert err and "not available" in text


async def test_plan_tools_read_compactly(ctx, seeded, settings, clk) -> None:
    svc = Services(settings, seeded, clk, FakeCanvasSource(), Recorder())
    pid = await propose_replan(svc, "first", send=False)
    await apply_choice(svc, pid, "A")
    text, err = await call(ctx, "get_plan", {"days": 2})
    assert not err and text.count("\n") == 1 and "Networks" in text
    text, err = await call(ctx, "get_progress")
    assert "Networks: 2 тем" in text and "Долг" in text
    text, _ = await call(ctx, "explain_priority", {"topic": "TCP"}, profile="plan")
    assert text.startswith("TCP: priority") and "stakes=" in text
    text, _ = await call(ctx, "explain_priority", {}, profile="plan")
    assert text.startswith("Top priorities today")


async def test_log_session_and_set_level(ctx, seeded) -> None:
    text, err = await call(
        ctx, "log_study_session", {"topic": "tcp", "minutes": 45, "kind": "practice"}
    )
    assert not err and "TCP" in text
    _, err = await call(ctx, "log_study_session", {"topic": "TCP", "minutes": 2000})
    assert err
    _, err = await call(
        ctx, "log_study_session", {"topic": "Quantum chromodynamics", "minutes": 30}
    )
    assert err
    text, err = await call(ctx, "set_topic_level", {"topic": "TCP", "level": 3})
    assert not err and "1 → 3" in text
    async with seeded() as s:
        t = await s.scalar(select(Topic).where(Topic.name == "TCP"))
        assert t.level == 3 and t.progress_min == 0
        assert len((await s.scalars(select(StudySession))).all()) == 1
    _, err = await call(ctx, "set_topic_level", {"topic": "TCP", "level": 7})
    assert err


async def test_manage_topics(ctx, seeded) -> None:
    assert not (
        await call(ctx, "manage_topics", {"action": "add", "exam": "Networks", "name": "Sockets"})
    )[1]
    assert not (
        await call(
            ctx, "manage_topics", {"action": "rename", "name": "Sockets", "new_name": "Socket API"}
        )
    )[1]
    assert not (
        await call(ctx, "manage_topics", {"action": "merge", "name": "Socket API", "into": "TCP"})
    )[1]
    assert not (await call(ctx, "manage_topics", {"action": "remove", "name": "Routing"}))[1]
    _, err = await call(ctx, "manage_topics", {"action": "explode", "name": "TCP"})
    assert err
    async with seeded() as s:
        statuses = {t.name: t.status for t in (await s.scalars(select(Topic))).all()}
    assert statuses["Socket API"] == "merged" and statuses["Routing"] == "dropped"


async def test_grade_tools(ctx, seeded, clk) -> None:
    async with seeded() as s:
        s.add(
            Course(
                id=101,
                name="Networks",
                course_code="CS3201",
                current_score=78.4,
                current_grade="B+",
                apply_group_weights=True,
                content_hash="x",
                synced_at=clk.now(),
            )
        )
        exam = await s.scalar(select(Exam).where(Exam.title == "Computer Networks"))
        exam.course_id = 101
        await s.commit()
    text, _ = await call(
        ctx, "calc_needed_final", {"course": "Networks", "target": "A-"}, profile="grades"
    )
    assert "80.7%" in text and "приблизительная" in text
    text, _ = await call(ctx, "calc_needed_final", {"course": "Algebra"}, profile="grades")
    assert "неизвестен" in text
    text, _ = await call(ctx, "get_course_scores", profile="grades")
    assert "78.4%" in text
    text, _ = await call(ctx, "get_deadlines", profile="grades")
    assert "нет" in text.lower()


async def test_agreements_and_reminders(ctx, seeded, clk) -> None:
    await call(
        ctx, "save_agreement", {"text": "Не трогать Network Layer до 25 ноября", "exam": "Networks"}
    )
    text, _ = await call(ctx, "list_agreements")
    assert "#1 Не трогать" in text
    when = (clk.now() + timedelta(hours=3)).astimezone(ctx.settings.zone).strftime("%Y-%m-%d %H:%M")
    _, err = await call(ctx, "add_personal_reminder", {"text": "Позвонить", "at": when})
    assert not err
    _, err = await call(ctx, "add_personal_reminder", {"text": "x", "at": "2020-01-01 10:00"})
    assert err
    text, _ = await call(ctx, "list_reminders")
    assert "Позвонить" in text
    async with seeded() as s:
        assert len((await s.scalars(select(Agreement))).all()) == 1
        assert len((await s.scalars(select(Reminder))).all()) == 1


async def test_settings_change_is_only_a_proposal_and_locked_keys_are_refused(ctx, seeded) -> None:
    _, err = await call(ctx, "propose_settings_change", {"key": "cap_hours", "value": 5})
    assert not err and ctx.proposals
    async with seeded() as s:
        assert await s.get(Setting, "cap_hours") is None  # nothing applied by the tool
        assert (await s.get(Proposal, ctx.proposals[0])).kind == "settings"
    for key in (
        "protected_sleep",
        "eve_review_only",
        "no_blocks_during_exams",
        "no_new_topics_eve",
    ):
        _, err = await call(ctx, "propose_settings_change", {"key": key, "value": False})
        assert err
    _, err = await call(ctx, "propose_settings_change", {"key": "cap_hours", "value": 99})
    assert err


async def test_undo_without_history_and_usage(ctx) -> None:
    text, _ = await call(ctx, "propose_undo", profile="plan")
    assert "Нечего отменять" in text
    text, _ = await call(ctx, "get_usage")
    assert text.startswith("LLM сегодня")


async def test_bad_dates_and_caps_in_plan_change(ctx) -> None:
    _, err = await call(
        ctx,
        "propose_plan_change",
        {"reason": "x", "pause_exam": "Networks", "pause_until": "25/11"},
        profile="plan",
    )
    assert err
    _, err = await call(
        ctx,
        "propose_plan_change",
        {"reason": "x", "day_caps": [{"date": "2026-11-25", "hours": 30}]},
        profile="plan",
    )
    assert err


def test_material_profile_can_search_course_files() -> None:
    from agent.brain.tools import PROFILES

    assert (
        "search_course_files" in PROFILES["material"] and "search_course_files" in PROFILES["chat"]
    )
