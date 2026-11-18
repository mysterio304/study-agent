"""Feasibility: is there enough study time before each exam's eve to close the gaps?

Exams are checked cumulatively in date order (the plan is joint): the work for exams 1..k must
fit into the capacity of days before exam k's eve (the eve itself is review-only). If it does
not, we never overload — we report the shortfall and the topics whose removal costs the least
priority per hour saved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from agent.planner.model import ExamInfo, PlanConfig, TopicInfo
from agent.planner.priority import exam_local_date, priority
from agent.planner.slots import day_capacity_blocks


def remaining_minutes(topic: TopicInfo) -> float:
    """Study minutes still needed to move `topic` from its level to its target level."""
    if topic.target_level <= 0 or topic.level >= topic.target_level:
        return 0.0
    share = (topic.target_level - topic.level) / topic.target_level
    return max(0.0, topic.effort_hours * 60 * share - topic.progress_min)


@dataclass(frozen=True, slots=True)
class DropCandidate:
    topic_id: int
    name: str
    exam_id: int
    hours_saved: float
    priority: float


@dataclass(frozen=True, slots=True)
class Feasibility:
    ok: bool
    shortfall_hours: float
    per_exam: Mapping[int, float] = field(
        default_factory=dict
    )  # exam_id → slack hours (neg = short)
    drop_candidates: tuple[DropCandidate, ...] = ()


def check_feasibility(
    exams: Sequence[ExamInfo], topics: Sequence[TopicInfo], cfg: PlanConfig, start: date
) -> Feasibility:
    ordered = sorted(exams, key=lambda e: e.starts_at)
    need_by_exam = {e.id: 0.0 for e in ordered}
    for t in topics:
        if t.exam_id in need_by_exam:
            need_by_exam[t.exam_id] += remaining_minutes(t)

    slack: dict[int, float] = {}
    cum_need, worst = 0.0, 0.0
    for e in ordered:
        eve = exam_local_date(e, cfg) - timedelta(days=1)
        cap = 0.0
        d = start
        while d < eve:
            cap += day_capacity_blocks(d, cfg, ordered) * cfg.block_min
            d += timedelta(days=1)
        cum_need += need_by_exam[e.id]
        slack[e.id] = round((cap - cum_need) / 60, 2)
        worst = max(worst, cum_need - cap)

    shortfall = round(max(0.0, worst) / 60, 2)
    drops: tuple[DropCandidate, ...] = ()
    if shortfall > 0:
        drops = _drop_candidates(ordered, topics, cfg, start, shortfall)
    return Feasibility(shortfall == 0, shortfall, slack, drops)


def _drop_candidates(
    exams: Sequence[ExamInfo],
    topics: Sequence[TopicInfo],
    cfg: PlanConfig,
    start: date,
    shortfall_hours: float,
) -> tuple[DropCandidate, ...]:
    by_id = {e.id: e for e in exams}
    scored = []
    for t in topics:
        hours = remaining_minutes(t) / 60
        if hours <= 0 or t.exam_id not in by_id:
            continue
        p = priority(t, by_id[t.exam_id], start, cfg).total
        scored.append(
            (p / hours, DropCandidate(t.id, t.name, t.exam_id, round(hours, 2), round(p, 3)))
        )
    scored.sort(key=lambda x: (x[0], x[1].topic_id))
    picked, saved = [], 0.0
    for _, cand in scored:
        if saved >= shortfall_hours:
            break
        picked.append(cand)
        saved += cand.hours_saved
    return tuple(picked)
