"""Planner test fixtures: the real exam schedule and a synthetic topic set."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from agent.core.timeutil import local_to_utc, parse_time_range
from agent.planner.model import ExamInfo, PlanConfig, TopicInfo

HKT = ZoneInfo("Asia/Hong_Kong")


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return local_to_utc(d, time(hh, mm), HKT)


NETWORKS, ALGO, ALGEBRA, CV = 1, 2, 3, 4


@pytest.fixture
def cfg() -> PlanConfig:
    return PlanConfig(
        zone=HKT,
        study_window=parse_time_range("09:00-23:00"),
        quiet_hours=parse_time_range("00:30-08:00"),
    )


@pytest.fixture
def exams() -> list[ExamInfo]:
    return [
        ExamInfo(NETWORKS, "Computer Networks", at(date(2026, 12, 8), 9, 30), 120, 0.7, "A-", 78.4),
        ExamInfo(ALGO, "Algorithms", at(date(2026, 12, 9), 18, 30), 120, 0.6, "A-", 82.0),
        ExamInfo(ALGEBRA, "Algebra", at(date(2026, 12, 12), 18, 30), 120, None, "A-", 71.5),
        ExamInfo(CV, "Computer Vision", at(date(2026, 12, 14), 14, 0), 120, 0.5, "A-", 88.0),
    ]


def _topics(
    exam_id: int, names: list[str], *, level: int = 0, effort: float = 3.0
) -> list[TopicInfo]:
    base = exam_id * 100
    return [
        TopicInfo(
            id=base + i,
            exam_id=exam_id,
            name=n,
            effort_hours=effort,
            importance=3 if i < 2 else 2,
            level=level,
            target_level=3 if i < 2 else 2,
        )
        for i, n in enumerate(names)
    ]


@pytest.fixture
def topics() -> list[TopicInfo]:
    return (
        _topics(
            NETWORKS,
            [
                "Application layer",
                "Transport/TCP",
                "Network layer",
                "Routing",
                "Link layer",
                "Wireless",
                "Security",
            ],
            level=1,
        )
        + _topics(
            ALGO,
            [
                "Divide & conquer",
                "Greedy",
                "Dynamic programming",
                "Graphs",
                "Network flow",
                "NP-completeness",
            ],
            level=1,
        )
        + _topics(
            ALGEBRA,
            ["Vector spaces", "Linear maps", "Eigenvalues", "Inner products"],
            level=2,
            effort=2.5,
        )
        + _topics(
            CV,
            ["Image formation", "Features", "Tracking", "Deep learning for CV"],
            level=2,
            effort=2.0,
        )
    )
