"""Outbox (dedupe, quiet hours, combining, digest), ladder, templates, dispatch, weekly report."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from agent.brain.llm import LlmClient
from agent.config import Settings
from agent.core.clock import FixedClock
from agent.core.timeutil import parse_time_range
from agent.db.models import Announcement, Assignment, Course, LlmUsage, OutboxMessage, StudySession
from agent.notify.events import (
    AssignmentRef,
    FileChanged,
    GradePosted,
    NewMessage,
)
from agent.notify.ladder import current_step, humanize, run_ladder
from agent.notify.outbox import enqueue, flush, mark_digest_sent, pending_digest, quiet_end
from agent.notify.report import streak
from agent.notify.templates import render
from tests.brain.conftest import FakeBackend, text_msg
from tests.notify.test_jobs import Recorder
from tests.planner.conftest import HKT, at

QUIET = parse_time_range("00:30-08:00")
SETTINGS = Settings(_env_file=None)


def clock_at(hh: int, mm: int = 0, day: int = 19) -> FixedClock:
    return FixedClock(at(date(2026, 11, day), hh, mm), HKT)


# --- outbox -----------------------------------------------------------------------------------


async def test_dedupe_and_quiet_hours(sessionmaker) -> None:
    night = clock_at(2)
    async with sessionmaker() as s:
        assert await enqueue(s, "k1", "normal msg", urgency="normal", clock=night, quiet=QUIET)
        assert not await enqueue(s, "k1", "again", urgency="normal", clock=night, quiet=QUIET)
        assert await enqueue(s, "k2", "urgent msg", urgency="urgent", clock=night, quiet=QUIET)
        await s.commit()
    rec = Recorder()
    await flush(sessionmaker, rec, night)
    assert rec.texts() == ["urgent msg"]  # normal waits for 08:00
    night.advance(hours=6)
    await flush(sessionmaker, rec, night)
    assert rec.texts()[-1] == "normal msg"
    await flush(sessionmaker, rec, night)
    assert len(rec.texts()) == 2  # nothing sent twice


def test_quiet_end_handles_wraparound() -> None:
    wrap = parse_time_range("23:00-07:00")
    end = quiet_end(at(date(2026, 11, 19), 23, 30), wrap, clock_at(23, 30))
    assert end == at(date(2026, 11, 20), 7, 0)
    assert quiet_end(at(date(2026, 11, 19), 12, 0), wrap, clock_at(12)) is None


async def test_many_plain_messages_are_combined_but_buttons_stay_separate(sessionmaker) -> None:
    c = clock_at(12)
    async with sessionmaker() as s:
        for i in range(5):
            await enqueue(s, f"p{i}", f"plain {i}", urgency="normal", clock=c, quiet=QUIET)
        await enqueue(
            s,
            "b",
            "with button",
            urgency="normal",
            clock=c,
            quiet=QUIET,
            buttons=[[("OK", "dl:1:done")]],
        )
        await s.commit()
    rec = Recorder()
    await flush(sessionmaker, rec, c)
    assert len(rec.messages) == 2
    assert all(f"plain {i}" in rec.messages[0][0] for i in range(5))
    assert rec.messages[1][1] is not None


async def test_digest_items_wait_for_morning(sessionmaker) -> None:
    c = clock_at(12)
    async with sessionmaker() as s:
        await enqueue(s, "f1", "📄 new file", urgency="digest", clock=c, quiet=QUIET)
        await s.commit()
    rec = Recorder()
    await flush(sessionmaker, rec, c)
    assert rec.messages == []
    async with sessionmaker() as s:
        rows = await pending_digest(s)
    assert [r.text for r in rows] == ["📄 new file"]
    await mark_digest_sent(sessionmaker, [r.id for r in rows], c)
    async with sessionmaker() as s:
        assert await pending_digest(s) == []


# --- ladder -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remaining", "step"),
    [
        (timedelta(days=8), None),
        (timedelta(days=6), "7d"),
        (timedelta(days=2), "3d"),
        (timedelta(hours=20), "24h"),
        (timedelta(hours=2), "3h"),
        (timedelta(minutes=-5), None),
    ],
)
def test_current_step(remaining: timedelta, step: str | None) -> None:
    now = datetime(2026, 11, 19, tzinfo=UTC)
    s = current_step(now + remaining, now)
    assert (s.name if s else None) == step


def test_humanize() -> None:
    assert humanize(timedelta(minutes=45)) == "45 мин"
    assert humanize(timedelta(hours=2, minutes=40)) == "2 ч 40 мин"
    assert humanize(timedelta(hours=30)) == "30 ч"
    assert humanize(timedelta(days=5, hours=3)) == "5 дн."


async def _course_and_assignment(s, c: FixedClock, *, due_in: timedelta, **over) -> Assignment:
    s.add(
        Course(
            id=101,
            name="Networks",
            course_code="CS3201",
            apply_group_weights=True,
            content_hash="x",
            synced_at=c.now(),
        )
    )
    await s.flush()  # no ORM relationships → flush the parent first for the FK
    fields = {"submission_types": ["online_upload"]} | over
    a = Assignment(
        id=1,
        course_id=101,
        name="Lab 5",
        due_at=c.now() + due_in,
        html_url="u",
        content_hash="x",
        synced_at=c.now(),
        **fields,
    )
    s.add(a)
    await s.flush()
    return a


async def test_ladder_sends_each_step_once_and_3h_is_urgent(sessionmaker) -> None:
    c = clock_at(2)  # quiet hours
    async with sessionmaker() as s:
        await _course_and_assignment(s, c, due_in=timedelta(hours=50))
        assert await run_ladder(s, c, QUIET) == 1
        assert await run_ladder(s, c, QUIET) == 0
        c.advance(hours=48)  # 2 h left, still quiet hours? 02:00 + 48h = 02:00 → quiet
        assert await run_ladder(s, c, QUIET) == 1
        await s.commit()
        rows = (await s.scalars(select(OutboxMessage).order_by(OutboxMessage.id))).all()
    assert [r.dedupe_key.split(":")[-1] for r in rows] == ["3d", "3h"]
    assert all(r.dedupe_key.startswith("deadline:1:") for r in rows)
    assert rows[1].urgency == "urgent" and rows[1].not_before == c.now()
    assert rows[0].not_before > at(date(2026, 11, 19), 7, 59)  # deferred past quiet hours


@pytest.mark.parametrize(
    "over",
    [
        {"submitted_at": datetime(2026, 11, 18, tzinfo=UTC)},
        {"user_state": "ignored"},
        {"user_state": "done"},
        {"excused": True},
        {"submission_types": ["on_paper"]},
    ],
)
async def test_ladder_skips_closed_work(sessionmaker, over) -> None:
    c = clock_at(12)
    async with sessionmaker() as s:
        await _course_and_assignment(s, c, due_in=timedelta(hours=20), **over)
        assert await run_ladder(s, c, QUIET) == 0


# --- templates & dispatch ---------------------------------------------------------------------


def test_templates_escape_html_and_have_stable_keys() -> None:
    ref = AssignmentRef(1, "CS<3201>", "Lab <script>", "https://x/?a=1&b=2", None, 10)
    r = render(GradePosted(ref, 9, "9", None, {"min": 3, "max": 10, "mean": 7.1}, "a < b"), HKT)
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text and "a &lt; b" in r.text
    assert "среднее 7.1" in r.text and r.key == "grade:1:9"
    msg = render(NewMessage(5, "Hi", "preview"), HKT)
    assert msg.key == render(NewMessage(5, "Hi", "preview"), HKT).key  # stable across calls
    assert render(FileChanged("CS3201", "L1.pdf", True), HKT).urgency == "digest"


async def test_long_announcements_are_summarised_once_short_ones_verbatim(sessionmaker) -> None:
    from agent.canvas.sync_runner import CanvasSync
    from agent.notify.dispatch import summarise
    from tests.canvas.test_sync_runner import MutableCanvas

    c = clock_at(12)
    canvas = MutableCanvas()
    llm = LlmClient(
        FakeBackend(text_msg("Экзамен перенесён в LT-5, 8 дек 09:30.")), sessionmaker, SETTINGS, c
    )
    sync = CanvasSync(sessionmaker, canvas, c, summarise=lambda t: summarise(llm, t))
    await sync.run()  # seeding run
    canvas.anns += [
        {
            "id": 1,
            "title": "Room",
            "message": "Final exam room changed. " * 40,
            "context_code": "course_101",
        },
        {"id": 2, "title": "Hi", "message": "Short note.", "context_code": "course_101"},
    ]
    await sync.run()
    await sync.run()  # nothing new → no new summaries, no duplicates
    async with sessionmaker() as s:
        assert (await s.get(Announcement, 1)).summary_ru.startswith("Экзамен перенесён")
        assert (await s.get(Announcement, 2)).summary_ru is None
        assert await s.scalar(select(func.count(LlmUsage.id))) == 1
        texts = [
            m.text
            for m in (await s.scalars(select(OutboxMessage).order_by(OutboxMessage.id))).all()
        ]
    assert "LT-5" in texts[0] and "Short note." in texts[1] and len(texts) == 2


# --- report -----------------------------------------------------------------------------------


def test_streak() -> None:
    today = date(2026, 11, 25)
    days = {date(2026, 11, d) for d in (21, 22, 23, 24)}
    assert streak(days, today) == 4  # today not yet studied → count up to yesterday
    assert streak(days | {today}, today) == 5
    assert streak({date(2026, 11, 20)}, today) == 0


async def test_weekly_job_sends_report(sessionmaker) -> None:
    from agent.canvas.fake import FakeCanvasSource
    from agent.db.models import Topic
    from agent.flows import Services, apply_choice, propose_replan
    from agent.notify import jobs
    from agent.prep.seed import seed_exams

    c = clock_at(20, day=22)
    async with sessionmaker() as s:
        await seed_exams(s)
        await s.flush()
        s.add(
            Topic(
                exam_id=1,
                name="TCP",
                effort_hours=3,
                importance=3,
                level=1,
                target_level=2,
                status="active",
            )
        )
        await s.flush()
        s.add(
            StudySession(topic_id=1, minutes=50, kind="learn", source="manual", created_at=c.now())
        )
        await s.commit()
    svc = Services(SETTINGS, sessionmaker, c, FakeCanvasSource(), Recorder())
    await apply_choice(svc, await propose_replan(svc, "x", send=False), "A")
    await jobs.weekly(svc)
    report = svc.notifier.texts()[-1]
    assert "Итоги недели" in report and "серия 1 дн." in report
