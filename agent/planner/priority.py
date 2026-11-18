"""Topic priority and exam phases (ARCHITECTURE §5).

    priority = Σ w_k · term_k, every term in [0, 1]

    stakes     = final_weight × clamp(needed_final% / 100, 0.3, 1.0)   (unknown weight → 0.5)
    gap        = (target_level − level) / 3
    importance = importance / 3
    urgency    = exp(−days_until_exam / 7)
    forgetting = 0 if no review due, else min(1, 0.5 + overdue_days / 3)

Weights live in `PlanConfig.weights`, so they are configurable without code changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from agent.planner.model import ExamInfo, Phase, PlanConfig, TopicInfo

STAKES_FLOOR = 0.3
UNKNOWN_NEED = 0.8  # when there is no current score, assume the final matters a lot


def needed_final_pct(
    current: float | None, final_weight: float | None, target: float
) -> float | None:
    """Final-exam % needed to reach `target` course %, assuming coursework stays at `current`."""
    if current is None or not final_weight:
        return None
    return (target - current * (1 - final_weight)) / final_weight


def stakes(exam: ExamInfo, cfg: PlanConfig) -> float:
    weight = exam.final_weight if exam.final_weight is not None else cfg.default_final_weight
    target = cfg.cutoffs.get(exam.target_grade, 80)
    needed = needed_final_pct(exam.current_score, weight, target)
    need = UNKNOWN_NEED if needed is None else needed / 100
    return weight * max(STAKES_FLOOR, min(1.0, need))


def exam_local_date(exam: ExamInfo, cfg: PlanConfig) -> date:
    return exam.starts_at.astimezone(cfg.zone).date()


def is_late_exam(exam: ExamInfo, cfg: PlanConfig) -> bool:
    """Afternoon/evening exam: the morning of the exam day can hold a final review."""
    return exam.starts_at.astimezone(cfg.zone).hour >= cfg.morning_exam_before_hour


def days_until(exam: ExamInfo, day: date, cfg: PlanConfig) -> int:
    return (exam_local_date(exam, cfg) - day).days


def phase_for(exam: ExamInfo, day: date, cfg: PlanConfig) -> Phase:
    left = days_until(exam, day, cfg)
    if left < 0:
        return "done"
    if left == 0:
        return "exam_day"
    if left == 1:
        return "review"
    if left < cfg.learn_phase_min_days:
        return "practice"
    return "learn"


def forgetting(topic: TopicInfo, day: date, cfg: PlanConfig) -> float:
    if topic.next_review_at is None:
        return 0.0
    due = topic.next_review_at.astimezone(cfg.zone).date()
    overdue = (day - due).days
    if overdue < 0:
        return 0.0
    return min(1.0, 0.5 + overdue / 3)


@dataclass(frozen=True, slots=True)
class PriorityBreakdown:
    total: float
    terms: dict[str, float]


def priority(
    topic: TopicInfo, exam: ExamInfo, day: date, cfg: PlanConfig, *, level: float | None = None
) -> PriorityBreakdown:
    """Priority of `topic` on `day`. `level` overrides the stored level (planner's virtual
    progress while allocating)."""
    lvl = topic.level if level is None else level
    terms = {
        "stakes": stakes(exam, cfg),
        "gap": max(0.0, min(1.0, (topic.target_level - lvl) / 3)),
        "importance": max(0.0, min(1.0, topic.importance / 3)),
        "urgency": math.exp(-max(0, days_until(exam, day, cfg)) / 7),
        "forgetting": forgetting(topic, day, cfg),
    }
    total = sum(cfg.weights[k] * v for k, v in terms.items())
    return PriorityBreakdown(total=total, terms=terms)
