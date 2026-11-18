"""Planner domain types. Pure data — no DB, no I/O — so the planner is fully unit-testable."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Literal
from zoneinfo import ZoneInfo

from agent.core.timeutil import TimeRange

BlockKind = Literal["learn", "practice", "review", "mock"]
Phase = Literal["learn", "practice", "review", "exam_day", "done"]

DEFAULT_WEIGHTS = MappingProxyType(
    {"stakes": 0.25, "gap": 0.30, "importance": 0.15, "urgency": 0.20, "forgetting": 0.10}
)
# Letter → minimum course percentage (user-confirmed for A-/A/A+, defaults below).
DEFAULT_CUTOFFS = MappingProxyType(
    {"A+": 90, "A": 85, "A-": 80, "B+": 75, "B": 70, "B-": 65, "C+": 60, "C": 55, "C-": 50, "D": 45}
)


@dataclass(frozen=True, slots=True)
class ExamInfo:
    id: int
    title: str
    starts_at: datetime  # aware UTC
    duration_min: int
    final_weight: float | None  # 0..1, None = unknown
    target_grade: str
    current_score: float | None  # course % so far (Canvas), None = unknown


@dataclass(frozen=True, slots=True)
class TopicInfo:
    id: int
    exam_id: int
    name: str
    effort_hours: float
    importance: int  # 1..3
    level: int  # 0..3
    target_level: int = 2
    last_studied_at: datetime | None = None
    next_review_at: datetime | None = None
    progress_min: int = 0  # minutes studied since the level last changed
    review_step: int = 0  # spaced-repetition step already reached (intervals 1, 3, 7, 14 days)


@dataclass(frozen=True, slots=True)
class Interval:
    start: datetime  # aware UTC
    end: datetime

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class Slot:
    """A candidate study block position (start/end UTC) on a local day."""

    day: date
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class PlannedBlock:
    start: datetime
    end: datetime
    exam_id: int
    topic_id: int | None  # None for mock exams
    kind: BlockKind

    def to_json(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "exam_id": self.exam_id,
            "topic_id": self.topic_id,
            "kind": self.kind,
        }

    @classmethod
    def from_json(cls, raw: dict[str, object]) -> PlannedBlock:
        return cls(
            start=datetime.fromisoformat(str(raw["start"])),
            end=datetime.fromisoformat(str(raw["end"])),
            exam_id=int(raw["exam_id"]),  # type: ignore[arg-type]
            topic_id=int(raw["topic_id"]) if raw.get("topic_id") is not None else None,  # type: ignore[arg-type]
            kind=raw["kind"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class PlanConfig:
    """All planner knobs. Defaults follow SPEC §6 and the user's answers (2026-11-18)."""

    zone: ZoneInfo
    study_window: TimeRange
    quiet_hours: TimeRange
    cap_hours: float = 6.5
    final_week_cap_hours: float = 8.5  # the 7 days before the first exam
    between_exams_cap_hours: float = 7.5  # after the first exam
    exam_day_cap_hours: float = 3.0  # light review on an exam day (other exam)
    block_min: int = 50
    break_min: int = 10
    long_break_min: int = 30
    blocks_before_long_break: int = 3
    lighter_weekday: int = 6  # Sunday
    lighter_factor: float = 0.5
    lighter_skip_if_exam_within_days: int = 3
    exam_buffer_before_min: int = 60
    exam_buffer_after_min: int = 30
    morning_exam_before_hour: int = 12  # exams starting earlier get protected sleep
    protected_sleep_last_block_end: str = "21:00"
    max_blocks_per_topic_per_day: int = 2
    review_intervals_days: tuple[int, ...] = (1, 3, 7, 14)
    learn_phase_min_days: int = 8  # days_left >= 8 → learn
    mock_days_before: tuple[int, ...] = (2, 4)
    weights: MappingProxyType = field(default_factory=lambda: DEFAULT_WEIGHTS)
    cutoffs: MappingProxyType = field(default_factory=lambda: DEFAULT_CUTOFFS)
    default_final_weight: float = 0.5
    # Non-negotiables (SPEC §3.5) — changeable only via the explicit /settings command.
    eve_review_only: bool = True
    no_new_topics_eve: bool = True
    protected_sleep: bool = True
    no_blocks_during_exams: bool = True
    # Agreed overrides (applied only via confirmed proposals):
    day_caps: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))  # date → h
    paused_until: MappingProxyType = field(
        default_factory=lambda: MappingProxyType({})
    )  # exam → date
