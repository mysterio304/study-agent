"""Seed data: the Semester A 2026/27 finals from SPEC.md §0 (times are HKT)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.core.timeutil import local_to_utc
from agent.db.models import Exam

HKT = ZoneInfo("Asia/Hong_Kong")
DEFAULT_TARGET_GRADE = "A-"


@dataclass(frozen=True, slots=True)
class FinalSeed:
    title: str
    day: date
    start: time
    duration_min: int
    final_weight: float | None  # None → find in syllabus or ask


FINALS: tuple[FinalSeed, ...] = (
    FinalSeed("Computer Networks", date(2026, 12, 8), time(9, 30), 120, 0.70),
    FinalSeed("Design and Analysis of Algorithms", date(2026, 12, 9), time(18, 30), 120, None),
    FinalSeed("Algebra", date(2026, 12, 12), time(18, 30), 120, None),
    FinalSeed(
        "Computer Vision for Interactivity (CS4187)", date(2026, 12, 14), time(14, 0), 120, None
    ),
)


async def seed_exams(session: AsyncSession) -> int:
    """Insert missing finals; never overwrite existing rows (they may hold user edits).
    Returns the number of exams created."""
    existing = set((await session.scalars(select(Exam.title))).all())
    created = 0
    for f in FINALS:
        if f.title in existing:
            continue
        session.add(
            Exam(
                title=f.title,
                starts_at=local_to_utc(f.day, f.start, HKT),
                duration_min=f.duration_min,
                final_weight=f.final_weight,
                weight_source="user" if f.final_weight is not None else "unknown",
                target_grade=DEFAULT_TARGET_GRADE,
            )
        )
        created += 1
    await session.flush()
    return created
