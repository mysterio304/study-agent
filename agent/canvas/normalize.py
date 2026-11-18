"""Raw Canvas JSON → compact, typed snapshots. Only fields we use survive, so nothing raw ever
reaches the DB diffing logic or the LLM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent.core.timeutil import parse_iso_utc

SUBMITTED_STATES = frozenset({"submitted", "graded", "pending_review"})
NON_SUBMITTABLE = frozenset({"none", "on_paper", "not_graded"})


@dataclass(frozen=True, slots=True)
class Comment:
    author_id: int | None
    author: str
    text: str
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class AssignmentSnap:
    id: int
    course_id: int
    group_id: int | None
    name: str
    due_at: datetime | None
    points_possible: float | None
    html_url: str | None
    submission_types: tuple[str, ...]
    published: bool
    omit_from_final_grade: bool
    workflow_state: str | None
    submitted_at: datetime | None
    score: float | None
    grade: str | None
    excused: bool
    is_missing: bool
    posted: bool
    comments: tuple[Comment, ...]
    stats: dict[str, float] | None

    @property
    def submitted(self) -> bool:
        return self.submitted_at is not None or (self.workflow_state or "") in SUBMITTED_STATES

    @property
    def needs_submission(self) -> bool:
        """Should the deadline ladder track this? (online submission expected, not done)"""
        online = bool(set(self.submission_types) - NON_SUBMITTABLE)
        return self.published and online and not self.submitted and not self.excused

    def hash_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "due": self.due_at and self.due_at.isoformat(),
            "pts": self.points_possible,
            "state": self.workflow_state,
            "sub": self.submitted_at and self.submitted_at.isoformat(),
            "score": self.score,
            "grade": self.grade,
            "missing": self.is_missing,
            "excused": self.excused,
            "comments": len(self.comments),
            "stats": self.stats,
            "group": self.group_id,
            "types": list(self.submission_types),
            "omit": self.omit_from_final_grade,
        }


def _comment(raw: dict[str, Any]) -> Comment:
    return Comment(
        author_id=raw.get("author_id"),
        author=str(raw.get("author_name") or "Преподаватель"),
        text=str(raw.get("comment") or "").strip(),
        created_at=parse_iso_utc(raw.get("created_at")),
    )


def _stats(raw: dict[str, Any] | None) -> dict[str, float] | None:
    if not raw:
        return None
    keys = ("min", "max", "mean", "median", "lower_q", "upper_q")
    out = {k: float(raw[k]) for k in keys if raw.get(k) is not None}
    return out or None


def assignment(
    raw: dict[str, Any], course_id: int, submission: dict[str, Any] | None = None
) -> AssignmentSnap:
    """`submission` (from /students/submissions, with comments) wins over the embedded one."""
    sub = submission or raw.get("submission") or {}
    comments = tuple(
        sorted(
            (_comment(c) for c in sub.get("submission_comments") or []),
            key=lambda c: c.created_at.timestamp() if c.created_at else 0.0,
        )
    )
    posted = sub.get("posted_at") is not None or sub.get("score") is not None
    return AssignmentSnap(
        id=int(raw["id"]),
        course_id=course_id,
        group_id=raw.get("assignment_group_id"),
        name=str(raw.get("name") or "Без названия")[:255],
        due_at=parse_iso_utc(raw.get("due_at")),
        points_possible=raw.get("points_possible"),
        html_url=raw.get("html_url"),
        submission_types=tuple(raw.get("submission_types") or ()),
        published=bool(raw.get("published", True)),
        omit_from_final_grade=bool(raw.get("omit_from_final_grade")),
        workflow_state=sub.get("workflow_state"),
        submitted_at=parse_iso_utc(sub.get("submitted_at")),
        score=sub.get("score") if posted else None,
        grade=sub.get("grade") if posted else None,
        excused=bool(sub.get("excused")),
        is_missing=bool(sub.get("missing")),
        posted=posted,
        comments=comments,
        stats=_stats(raw.get("score_statistics")),
    )
