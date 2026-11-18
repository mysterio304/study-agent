"""Full Canvas sync: silent first run, then events for real changes; failing scopes isolated."""

import copy
from datetime import date

import pytest
from sqlalchemy import select

from agent.canvas.client import CanvasAccessDenied
from agent.canvas.fake import FakeCanvasSource
from agent.canvas.sync_runner import CanvasSync
from agent.core.clock import FixedClock
from agent.db.models import Assignment, AssignmentGroup, CourseFile, SyncState
from agent.notify.events import (
    AssignmentAdded,
    DueChanged,
    FileChanged,
    GradePosted,
    MissingFlagged,
    NewAnnouncement,
    NewComment,
    NewMessage,
)
from tests.planner.conftest import HKT, at


class MutableCanvas(FakeCanvasSource):
    """Fixture courses + in-memory assignments/announcements/etc. that tests can mutate."""

    def __init__(self) -> None:
        super().__init__()
        self.assignments = {
            101: [
                {
                    "id": 1,
                    "name": "Lab 5",
                    "due_at": "2026-11-25T15:59:00Z",
                    "points_possible": 10,
                    "html_url": "u1",
                    "submission_types": ["online_upload"],
                    "published": True,
                    "assignment_group_id": 11,
                    "score_statistics": None,
                },
            ]
        }
        self.subs = {
            101: [{"assignment_id": 1, "workflow_state": "unsubmitted", "submission_comments": []}]
        }
        self.groups = {
            101: [
                {"id": 11, "name": "Labs", "group_weight": 30},
                {"id": 12, "name": "Final Exam", "group_weight": 70},
            ]
        }
        self.anns: list[dict] = []
        self.convs: list[dict] = []
        self.files = {
            101: [
                {"id": 501, "display_name": "Lecture 1.pdf", "updated_at": "2026-09-01T00:00:00Z"}
            ]
        }
        self.missing: list[dict] = []
        self.fail_files = False

    async def list_assignments(self, course_id):
        return copy.deepcopy(self.assignments.get(course_id, []))

    async def list_my_submissions(self, course_id):
        return copy.deepcopy(self.subs.get(course_id, []))

    async def list_assignment_groups(self, course_id):
        return copy.deepcopy(self.groups.get(course_id, []))

    async def list_announcements(self, course_ids, since):
        return copy.deepcopy(self.anns)

    async def list_conversations(self):
        return copy.deepcopy(self.convs)

    async def list_files(self, course_id):
        if self.fail_files:
            raise CanvasAccessDenied("hidden", 403)
        return copy.deepcopy(self.files.get(course_id))

    async def list_missing_submissions(self):
        return copy.deepcopy(self.missing)


@pytest.fixture
def clk():
    return FixedClock(at(date(2026, 11, 19), 10), HKT)


async def test_first_run_is_silent_then_changes_produce_events(sessionmaker, clk) -> None:
    canvas = MutableCanvas()
    sync = CanvasSync(sessionmaker, canvas, clk)
    first = await sync.run()
    assert first.events == [] and first.failed_scopes == []
    async with sessionmaker() as s:
        assert (await s.get(Assignment, 1)).due_at is not None
        assert len((await s.scalars(select(AssignmentGroup))).all()) == 2
        assert (await s.get(SyncState, "assignments:101")).initialized

    # Changes on Canvas
    canvas.assignments[101][0]["due_at"] = "2026-11-27T15:59:00Z"
    canvas.assignments[101].append(
        {
            "id": 2,
            "name": "Lab 6",
            "due_at": "2026-12-01T15:59:00Z",
            "points_possible": 10,
            "submission_types": ["online_upload"],
            "published": True,
            "assignment_group_id": 11,
        }
    )
    canvas.anns.append(
        {
            "id": 900,
            "title": "Room change",
            "message": "<p>Final in LT-5</p>",
            "context_code": "course_101",
            "html_url": "a",
            "posted_at": "2026-11-19T01:00:00Z",
        }
    )
    canvas.convs.append(
        {
            "id": 77,
            "subject": "Re: Lab 5",
            "last_message": "Please resubmit",
            "last_message_at": "2026-11-19T01:00:00Z",
            "workflow_state": "unread",
            "participants": [{"name": "Dr. Lee"}],
        }
    )
    events = (await sync.run()).events
    kinds = {type(e) for e in events}
    assert {DueChanged, AssignmentAdded, NewAnnouncement, NewMessage} <= kinds

    # Grade with teacher comment; then a new comment only; then missing
    canvas.subs[101][0] = {
        "assignment_id": 1,
        "workflow_state": "graded",
        "score": 8,
        "grade": "8",
        "posted_at": "x",
        "submitted_at": "2026-11-20T00:00:00Z",
        "submission_comments": [
            {
                "author_id": 99,
                "author_name": "TA",
                "comment": "ok",
                "created_at": "2026-11-21T00:00:00Z",
            }
        ],
    }
    [grade] = [e for e in (await sync.run()).events if isinstance(e, GradePosted)]
    assert grade.score == 8 and grade.comment == "ok"
    canvas.subs[101][0]["submission_comments"].append(
        {
            "author_id": 99,
            "author_name": "TA",
            "comment": "see me",
            "created_at": "2026-11-22T00:00:00Z",
        }
    )
    assert any(isinstance(e, NewComment) and e.text == "see me" for e in (await sync.run()).events)
    canvas.missing.append({"id": 2, "name": "Lab 6", "course_id": 101})
    assert any(isinstance(e, MissingFlagged) for e in (await sync.run()).events)
    assert not any(isinstance(e, MissingFlagged) for e in (await sync.run()).events)  # once only


async def test_files_scope_is_hourly_and_reports_new_and_updated(sessionmaker, clk) -> None:
    canvas = MutableCanvas()
    sync = CanvasSync(sessionmaker, canvas, clk)
    await sync.run()
    canvas.files[101].append(
        {"id": 502, "display_name": "Lecture 2.pdf", "updated_at": "2026-11-19T00:00:00Z"}
    )
    assert not any(isinstance(e, FileChanged) for e in (await sync.run()).events)  # < 1 h later
    clk.advance(hours=1, minutes=1)
    canvas.files[101][0]["updated_at"] = "2026-11-19T01:00:00Z"
    files = [e for e in (await sync.run()).events if isinstance(e, FileChanged)]
    assert {(f.name, f.is_new) for f in files} == {
        ("Lecture 2.pdf", True),
        ("Lecture 1.pdf", False),
    }
    async with sessionmaker() as s:
        row = await s.scalar(select(CourseFile).where(CourseFile.canvas_file_id == 501))
        assert row.index_state == "pending"  # will be re-indexed (M5)


async def test_failing_scope_is_isolated_and_recorded(sessionmaker, clk) -> None:
    canvas = MutableCanvas()
    canvas.fail_files = True
    report = await CanvasSync(sessionmaker, canvas, clk).run()
    assert "files:101" in report.failed_scopes
    async with sessionmaker() as s:
        assert (await s.get(SyncState, "files:101")).consecutive_failures == 1
        assert (await s.get(SyncState, "assignments:101")).initialized  # others still ran
