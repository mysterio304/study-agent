"""Pure diffing: previous stored state vs a fresh snapshot → domain events.

Kept free of I/O so every rule is unit-tested. The sync layer decides whether events are
emitted at all (the first sync of each scope seeds state silently).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.canvas.normalize import AssignmentSnap
from agent.notify.events import (
    AssignmentAdded,
    AssignmentRef,
    DueChanged,
    Event,
    GradePosted,
    MissingFlagged,
    NewComment,
)

DUE_CHANGE_TOLERANCE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class StoredAssignment:
    """What we remember about an assignment from the previous sync."""

    due_at: datetime | None
    score: float | None
    comment_count: int
    is_missing: bool
    user_state: str | None = None  # «Сделал» / «Игнор» from the deadline buttons


def ref(snap: AssignmentSnap, course_name: str) -> AssignmentRef:
    return AssignmentRef(
        snap.id, course_name, snap.name, snap.html_url, snap.due_at, snap.points_possible
    )


def diff_assignment(
    old: StoredAssignment | None,
    new: AssignmentSnap,
    *,
    course_name: str,
    self_id: int | None,
    now: datetime,
) -> list[Event]:
    r = ref(new, course_name)
    if not new.published:
        return []
    if old is None:
        if new.score is not None:  # appeared already graded (after the scope's seeding run)
            return [_grade(r, new, None, self_id)]
        future = new.due_at is not None and new.due_at > now
        return [AssignmentAdded(r)] if future and new.needs_submission else []

    events: list[Event] = []
    silenced = old.user_state is not None  # the student closed this one with a button
    if _due_changed(old.due_at, new.due_at) and new.needs_submission and not silenced:
        events.append(DueChanged(r, old.due_at))
    if new.score is not None and new.score != old.score:
        events.append(_grade(r, new, old.score, self_id))
    elif len(new.comments) > old.comment_count:
        fresh = [c for c in new.comments[old.comment_count :] if c.author_id != self_id and c.text]
        if fresh:
            events.append(NewComment(r, fresh[-1].author, fresh[-1].text))
    if new.is_missing and not old.is_missing and not new.submitted and not silenced:
        events.append(MissingFlagged(r))
    return events


def _grade(
    r: AssignmentRef, new: AssignmentSnap, previous: float | None, self_id: int | None
) -> GradePosted:
    others = [c for c in new.comments if c.author_id != self_id and c.text]
    assert new.score is not None
    return GradePosted(
        r, new.score, new.grade, previous, new.stats, others[-1].text if others else None
    )


def _due_changed(old: datetime | None, new: datetime | None) -> bool:
    if old is None or new is None:
        return old != new and new is not None
    return abs(new - old) > DUE_CHANGE_TOLERANCE
