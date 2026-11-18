"""Non-negotiable checks (SPEC §3.5) run on every candidate plan before it can be proposed.

A plan with violations is never shown to the user as an option; tests assert that the allocator
produces none.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from agent.planner.model import ExamInfo, Interval, PlanConfig, PlannedBlock, TopicInfo
from agent.planner.priority import exam_local_date
from agent.planner.slots import day_capacity_blocks, forbidden_intervals


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    message: str
    block: PlannedBlock | None = None


def _local_day(b: PlannedBlock, cfg: PlanConfig) -> date:
    return b.start.astimezone(cfg.zone).date()


def validate_plan(
    blocks: Sequence[PlannedBlock],
    exams: Sequence[ExamInfo],
    topics: Sequence[TopicInfo],
    cfg: PlanConfig,
    *,
    busy: Sequence[Interval],
) -> list[Violation]:
    out: list[Violation] = []
    if not blocks:
        return out
    exam_by_id = {e.id: e for e in exams}
    level0 = {t.id for t in topics if t.level == 0}
    ordered = sorted(blocks, key=lambda b: b.start)
    days: dict[date, list[PlannedBlock]] = defaultdict(list)
    for b in ordered:
        days[_local_day(b, cfg)].append(b)

    for day, items in days.items():
        forbidden = forbidden_intervals(day, cfg, exams, busy)
        for b in items:
            iv = Interval(b.start, b.end)
            if any(f.overlaps(iv) for f in forbidden):
                out.append(
                    Violation("forbidden_time", "block in exam/busy/protected-sleep time", b)
                )
            if cfg.quiet_hours.contains(b.start.astimezone(cfg.zone).time()):
                out.append(Violation("quiet_hours", "block during quiet hours", b))
            exam = exam_by_id.get(b.exam_id)
            if exam and b.end > exam.starts_at:
                out.append(Violation("after_exam", "block for an exam that already started", b))
        if len(items) > day_capacity_blocks(day, cfg, exams):
            out.append(Violation("over_cap", f"{day}: {len(items)} blocks exceed the daily cap"))
        per_topic = Counter(b.topic_id for b in items if b.topic_id is not None)
        if per_topic and max(per_topic.values()) > cfg.max_blocks_per_topic_per_day:
            out.append(Violation("topic_overuse", f"{day}: a topic exceeds the per-day limit"))

    first_day = _local_day(ordered[0], cfg)
    for exam in exams:
        eve = exam_local_date(exam, cfg) - timedelta(days=1)
        if eve < first_day or not cfg.eve_review_only:
            continue
        eve_blocks = days.get(eve, [])
        if not any(b.exam_id == exam.id and b.kind == "review" for b in eve_blocks):
            out.append(Violation("missing_eve_review", f"no review the day before {exam.title}"))
        for b in eve_blocks:
            if b.exam_id != exam.id or b.kind != "review":
                out.append(Violation("eve_not_review", f"eve of {exam.title} must be review", b))
            if (
                cfg.no_new_topics_eve
                and b.topic_id in level0
                and not any(
                    x.topic_id == b.topic_id and x.start < b.start and _local_day(x, cfg) < eve
                    for x in ordered
                )
            ):
                out.append(Violation("new_topic_eve", "new topic the day before an exam", b))
    return out
