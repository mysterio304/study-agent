"""Domain events produced by the Canvas differ and consumed by notification templates."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AssignmentRef:
    id: int
    course: str
    name: str
    url: str | None
    due_at: datetime | None
    points_possible: float | None


@dataclass(frozen=True, slots=True)
class AssignmentAdded:
    a: AssignmentRef


@dataclass(frozen=True, slots=True)
class DueChanged:
    a: AssignmentRef
    old_due: datetime | None


@dataclass(frozen=True, slots=True)
class GradePosted:
    a: AssignmentRef
    score: float
    grade: str | None
    previous_score: float | None
    stats: dict[str, float] | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class NewComment:
    a: AssignmentRef
    author: str
    text: str


@dataclass(frozen=True, slots=True)
class MissingFlagged:
    a: AssignmentRef


@dataclass(frozen=True, slots=True)
class NewAnnouncement:
    id: int
    course: str
    title: str
    text: str  # plain text (HTML stripped)
    url: str | None


@dataclass(frozen=True, slots=True)
class FileChanged:
    course: str
    name: str
    is_new: bool
    stamp: str = ""  # Canvas updated_at of this version (dedupe key for updates)
    file_id: int = 0  # Canvas file id (two files may share a name)


@dataclass(frozen=True, slots=True)
class NewMessage:
    id: int
    subject: str
    preview: str
    participants: list[str] = field(default_factory=list)


Event = (
    AssignmentAdded
    | DueChanged
    | GradePosted
    | NewComment
    | MissingFlagged
    | NewAnnouncement
    | FileChanged
    | NewMessage
)
