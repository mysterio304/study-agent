"""Regression tests for findings from the Milestone 3 code review."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from agent.canvas.differ import StoredAssignment, diff_assignment
from agent.canvas.normalize import assignment
from agent.canvas.sync_runner import CanvasSync
from agent.core.clock import FixedClock
from agent.core.timeutil import parse_time_range
from agent.db.models import Assignment, Course, OutboxMessage
from agent.grades.calculator import AssignmentIn, GroupIn, needed_on_final
from agent.grades.service import deadlines_view
from agent.notify import templates
from agent.notify.events import DueChanged, MissingFlagged
from agent.notify.ladder import run_ladder
from agent.notify.outbox import enqueue, flush
from agent.notify.recheck import refresh_message
from tests.canvas.test_sync_runner import MutableCanvas
from tests.notify.test_jobs import Recorder
from tests.planner.conftest import HKT, at

QUIET = parse_time_range("00:30-08:00")


def clk(hh: int = 12, day: int = 19) -> FixedClock:
    return FixedClock(at(date(2026, 11, day), hh), HKT)


async def test_notifications_are_queued_in_the_sync_transaction(sessionmaker) -> None:
    c = clk()
    canvas = MutableCanvas()
    sync = CanvasSync(sessionmaker, canvas, c)
    await sync.run()
    canvas.convs.append(
        {
            "id": 1,
            "subject": "Hi",
            "last_message": "x",
            "last_message_at": "2026-11-19T03:00:00Z",
            "workflow_state": "unread",
        }
    )
    await sync.run()
    async with sessionmaker() as s:  # no separate dispatch step needed — already in the outbox
        assert await s.scalar(select(func.count(OutboxMessage.id))) == 1


async def test_one_bad_event_does_not_drop_the_others(sessionmaker, monkeypatch) -> None:
    c = clk()
    canvas = MutableCanvas()
    sync = CanvasSync(sessionmaker, canvas, c)
    await sync.run()
    original = templates.render

    def flaky(event, zone, **kw):
        if getattr(event, "subject", "") == "boom":
            raise RuntimeError("render failed")
        return original(event, zone, **kw)

    monkeypatch.setattr("agent.notify.dispatch.render", flaky)
    canvas.convs += [
        {
            "id": 1,
            "subject": "boom",
            "last_message": "x",
            "last_message_at": "2026-11-19T03:00:00Z",
            "workflow_state": "unread",
        },
        {
            "id": 2,
            "subject": "fine",
            "last_message": "y",
            "last_message_at": "2026-11-19T03:00:00Z",
            "workflow_state": "unread",
        },
    ]
    await sync.run()
    async with sessionmaker() as s:
        keys = [m.dedupe_key for m in (await s.scalars(select(OutboxMessage))).all()]
    assert len(keys) == 1 and keys[0].startswith("msg:2:")


async def _seed(s, c, **over) -> Assignment:
    s.add(
        Course(
            id=101,
            name="N",
            course_code="CS3201",
            apply_group_weights=True,
            content_hash="x",
            synced_at=c.now(),
        )
    )
    await s.flush()
    fields = {"submission_types": ["online_upload"], "due_at": c.now() + timedelta(hours=20)} | over
    a = Assignment(id=1, course_id=101, name="Lab", content_hash="x", synced_at=c.now(), **fields)
    s.add(a)
    await s.flush()
    return a


async def test_extended_deadline_gets_a_fresh_ladder(sessionmaker) -> None:
    c = clk()
    async with sessionmaker() as s:
        a = await _seed(s, c)
        assert await run_ladder(s, c, QUIET) == 1  # 24h step
        a.due_at = c.now() + timedelta(hours=22)  # extended, still in the 24h window
        assert await run_ladder(s, c, QUIET) == 1  # new due date → new key


@pytest.mark.parametrize("change", ["submitted", "done", "passed"])
async def test_queued_deadline_is_cancelled_at_send_time(sessionmaker, change) -> None:
    c = clk(hh=2)  # quiet hours: the 24h step waits until 08:00
    async with sessionmaker() as s:
        a = await _seed(s, c, due_at=c.now() + timedelta(hours=10))
        await run_ladder(s, c, QUIET)
        if change == "submitted":
            a.submitted_at = c.now()
        elif change == "done":
            a.user_state = "done"
        await s.commit()
    c.advance(hours=6 if change != "passed" else 11)
    rec = Recorder()
    await flush(sessionmaker, rec, c, refresh=refresh_message)
    assert rec.messages == []
    async with sessionmaker() as s:
        assert (await s.scalar(select(OutboxMessage))).status == "cancelled"


async def test_deadline_countdown_is_recomputed_when_sent(sessionmaker) -> None:
    c = clk(hh=2)
    async with sessionmaker() as s:
        await _seed(s, c, due_at=c.now() + timedelta(hours=12))
        await run_ladder(s, c, QUIET)
        await s.commit()
        assert "12 ч" in (await s.scalar(select(OutboxMessage))).text
    c.advance(hours=6)
    rec = Recorder()
    await flush(sessionmaker, rec, c, refresh=refresh_message)
    assert "через <b>6 ч</b>" in rec.texts()[0]


def test_done_or_ignored_silences_due_change_and_missing() -> None:
    now = datetime(2026, 11, 19, tzinfo=UTC)
    raw = {
        "id": 1,
        "name": "L",
        "due_at": "2026-11-27T00:00:00Z",
        "submission_types": ["online_upload"],
        "published": True,
    }
    snap = assignment(raw, 1, {"workflow_state": "unsubmitted", "missing": True})
    open_ = StoredAssignment(datetime(2026, 11, 25, tzinfo=UTC), None, 0, False)
    closed = StoredAssignment(datetime(2026, 11, 25, tzinfo=UTC), None, 0, False, "ignored")
    kinds = {type(e) for e in diff_assignment(open_, snap, course_name="C", self_id=None, now=now)}
    assert {DueChanged, MissingFlagged} <= kinds
    assert diff_assignment(closed, snap, course_name="C", self_id=None, now=now) == []


async def test_digest_survives_a_failed_send(sessionmaker) -> None:
    from agent.canvas.fake import FakeCanvasSource
    from agent.config import Settings
    from agent.flows import Services
    from agent.notify import jobs
    from agent.notify.outbox import pending_digest

    class Down(Recorder):
        async def send(self, text, kb=None):
            return None

    c = clk(hh=8)
    async with sessionmaker() as s:
        await enqueue(s, "f1", "📄 file", urgency="digest", clock=c, quiet=QUIET)
        await s.commit()
    await jobs.morning(
        Services(Settings(_env_file=None), sessionmaker, c, FakeCanvasSource(), Down())
    )
    async with sessionmaker() as s:
        assert [r.text for r in await pending_digest(s)] == ["📄 file"]


async def test_deadlines_view_marks_submitted_past_work_done(sessionmaker) -> None:
    c = clk()
    async with sessionmaker() as s:
        await _seed(
            s, c, due_at=c.now() - timedelta(days=1), submitted_at=c.now() - timedelta(days=2)
        )
        text = await deadlines_view(s, c)
    assert "✅" in text and "просрочено" not in text


def test_midterm_exam_is_not_mistaken_for_the_final() -> None:
    groups = [
        GroupIn(1, "Labs", 30),
        GroupIn(2, "Midterm Exam", 20),
        GroupIn(3, "Final Examination", 50),
    ]
    items = [AssignmentIn(1, 10, 8), AssignmentIn(2, 100, None), AssignmentIn(3, 100, None)]
    r = needed_on_final(
        groups, items, weighted=True, exam_final_weight=None, canvas_current=None, cutoffs={"A": 85}
    )
    assert r.final_weight == pytest.approx(0.5)


async def test_new_course_announcements_are_seeded_silently(sessionmaker) -> None:
    c = clk()
    canvas = MutableCanvas()
    sync = CanvasSync(sessionmaker, canvas, c)
    await sync.run()
    async with sessionmaker() as s:  # a course appears mid-term with old announcements
        s.add(
            Course(
                id=999, name="New", apply_group_weights=False, content_hash="x", synced_at=c.now()
            )
        )
        await s.commit()
    canvas.anns.append({"id": 5, "title": "old", "message": "x", "context_code": "course_999"})
    await sync.run()
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count(OutboxMessage.id))) == 0
