"""Normalizer + differ rules (pure)."""

from datetime import UTC, datetime

from agent.canvas.differ import StoredAssignment, diff_assignment
from agent.canvas.normalize import assignment
from agent.notify.events import AssignmentAdded, DueChanged, GradePosted, MissingFlagged, NewComment

NOW = datetime(2026, 11, 19, 4, 0, tzinfo=UTC)
ME = 7


def raw(**over):
    base = {
        "id": 1,
        "name": "Lab 5",
        "due_at": "2026-11-25T15:59:00Z",
        "points_possible": 10,
        "html_url": "https://canvas.example.edu/courses/1/assignments/1",
        "submission_types": ["online_upload"],
        "published": True,
        "assignment_group_id": 3,
        "submission": {"workflow_state": "unsubmitted"},
    }
    return base | over


def sub(**over):
    return {"workflow_state": "unsubmitted", "submission_comments": []} | over


def diff(old, new):
    return diff_assignment(old, new, course_name="CS3201", self_id=ME, now=NOW)


def stored(**over):
    return StoredAssignment(
        **(
            {
                "due_at": datetime(2026, 11, 25, 15, 59, tzinfo=UTC),
                "score": None,
                "comment_count": 0,
                "is_missing": False,
            }
            | over
        )
    )


def test_normalizer_keeps_only_needed_fields_and_utc() -> None:
    s = assignment(raw(score_statistics={"min": 2, "max": 10, "mean": 7.5}), 101)
    assert s.due_at == datetime(2026, 11, 25, 15, 59, tzinfo=UTC)
    assert s.needs_submission and not s.submitted
    assert s.stats == {"min": 2.0, "max": 10.0, "mean": 7.5}


def test_on_paper_and_submitted_are_not_tracked() -> None:
    assert not assignment(raw(submission_types=["on_paper"]), 1).needs_submission
    done = assignment(
        raw(), 1, sub(workflow_state="submitted", submitted_at="2026-11-20T10:00:00Z")
    )
    assert done.submitted and not done.needs_submission


def test_new_future_assignment_is_announced_once() -> None:
    assert isinstance(diff(None, assignment(raw(), 1))[0], AssignmentAdded)
    past = assignment(raw(due_at="2026-11-01T00:00:00Z"), 1)
    assert diff(None, past) == []


def test_due_date_change() -> None:
    moved = assignment(raw(due_at="2026-11-27T15:59:00Z"), 1)
    [e] = diff(stored(), moved)
    assert isinstance(e, DueChanged) and e.old_due == datetime(2026, 11, 25, 15, 59, tzinfo=UTC)
    same = assignment(raw(due_at="2026-11-25T15:59:30Z"), 1)
    assert diff(stored(), same) == []


def test_grade_posted_with_teacher_comment_and_stats() -> None:
    graded = assignment(
        raw(score_statistics={"min": 3, "max": 10, "mean": 7.1}),
        1,
        sub(
            workflow_state="graded",
            score=9,
            grade="9",
            posted_at="2026-11-26T00:00:00Z",
            submitted_at="2026-11-24T00:00:00Z",
            submission_comments=[
                {
                    "author_id": ME,
                    "author_name": "Me",
                    "comment": "submitted v2",
                    "created_at": "2026-11-24T00:00:00Z",
                },
                {
                    "author_id": 99,
                    "author_name": "Dr. Lee",
                    "comment": "Nice analysis",
                    "created_at": "2026-11-26T00:00:00Z",
                },
            ],
        ),
    )
    [e] = diff(stored(comment_count=1), graded)
    assert isinstance(e, GradePosted)
    assert (e.score, e.comment, e.previous_score) == (9, "Nice analysis", None)
    assert e.stats == {"min": 3.0, "max": 10.0, "mean": 7.1}


def test_scores_are_visible_only_when_canvas_returns_them() -> None:
    # Canvas returns score=null to students for unposted grades; a returned score is visible.
    hidden = assignment(raw(), 1, sub(workflow_state="graded", score=None, posted_at=None))
    assert hidden.score is None and not hidden.posted
    shown = assignment(raw(), 1, sub(workflow_state="graded", score=9, posted_at=None))
    assert shown.score == 9 and shown.posted


def test_new_comment_from_teacher_only() -> None:
    mine = assignment(
        raw(),
        1,
        sub(
            submission_comments=[
                {
                    "author_id": ME,
                    "author_name": "Me",
                    "comment": "question",
                    "created_at": "2026-11-20T00:00:00Z",
                }
            ]
        ),
    )
    assert diff(stored(), mine) == []
    teacher = assignment(
        raw(),
        1,
        sub(
            submission_comments=[
                {
                    "author_id": ME,
                    "author_name": "Me",
                    "comment": "question",
                    "created_at": "2026-11-20T00:00:00Z",
                },
                {
                    "author_id": 99,
                    "author_name": "TA",
                    "comment": "see slide 4",
                    "created_at": "2026-11-20T01:00:00Z",
                },
            ]
        ),
    )
    [e] = diff(stored(comment_count=1), teacher)
    assert isinstance(e, NewComment) and e.author == "TA" and e.text == "see slide 4"


def test_missing_flag_transition() -> None:
    missing = assignment(raw(), 1, sub(missing=True))
    assert isinstance(diff(stored(), missing)[0], MissingFlagged)
    assert diff(stored(is_missing=True), missing) == []


def test_unpublished_assignments_are_ignored() -> None:
    assert diff(None, assignment(raw(published=False), 1)) == []


def test_assignment_first_seen_already_graded_notifies_grade() -> None:
    graded = assignment(raw(), 1, sub(workflow_state="graded", score=7, grade="7", posted_at="x"))
    [e] = diff(None, graded)
    assert isinstance(e, GradePosted) and e.previous_score is None
