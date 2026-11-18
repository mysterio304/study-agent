"""Joint multi-exam allocator (ARCHITECTURE §5).

Day by day, from now until the last exam:
1. compute the day's slots (rhythm, caps, forbidden time);
2. decide which exams are eligible (the eve of an exam is reserved for that exam only);
3. place at most one mock exam (2 consecutive slots) when an exam is 2 or 4 days away;
4. reserve one "keep warm" block for every eligible exam that had no block yesterday;
5. fill the rest greedily by priority, re-scoring after every pick with virtual progress
   (remaining effort shrinks, spaced-repetition reviews become due).

Everything is deterministic: ties break on ids, never on dict/set order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

from agent.core.timeutil import local_to_utc
from agent.planner.feasibility import Feasibility, check_feasibility, remaining_minutes
from agent.planner.model import (
    BlockKind,
    ExamInfo,
    Interval,
    PlanConfig,
    PlannedBlock,
    Slot,
    TopicInfo,
)
from agent.planner.priority import days_until, exam_local_date, is_late_exam, phase_for, priority
from agent.planner.slots import day_slots

REVIEW_DUE_BONUS = 0.15
STABILITY_SLOT_BONUS = 0.06  # same topic in the same slot as the current plan
STABILITY_DAY_BONUS = 0.03  # same topic on the same day
EXTRA_PRACTICE_FACTOR = 0.3  # score multiplier once a topic's effort is used up
MOCK_SLOTS = 2


@dataclass
class _TopicState:
    topic: TopicInfo
    initial_remaining: float
    remaining: float
    studied: bool
    review_step: int
    next_review: date | None
    blocks_today: int = 0

    def virtual_level(self) -> float:
        t = self.topic
        if self.initial_remaining <= 0:
            return float(t.level)
        done = 1 - self.remaining / self.initial_remaining
        return t.level + (t.target_level - t.level) * done


@dataclass(frozen=True, slots=True)
class PlanResult:
    blocks: tuple[PlannedBlock, ...]
    feasibility: Feasibility
    unscheduled_hours: float = 0.0
    notes: tuple[str, ...] = field(default=())


def _init_states(topics: Sequence[TopicInfo], cfg: PlanConfig) -> dict[int, _TopicState]:
    states = {}
    for t in sorted(topics, key=lambda x: x.id):
        # Baseline = effort for the whole level gap, so minutes already logged (progress_min)
        # raise the virtual level exactly as if the planner had allocated them itself.
        total = remaining_minutes(replace(t, progress_min=0))
        nxt = t.next_review_at.astimezone(cfg.zone).date() if t.next_review_at else None
        studied = t.level > 0 or t.progress_min > 0
        states[t.id] = _TopicState(t, total, remaining_minutes(t), studied, t.review_step, nxt)
    return states


def _eligible_exams(
    day: date, exams: Sequence[ExamInfo], cfg: PlanConfig
) -> tuple[list[ExamInfo], bool]:
    """(eligible exams, focus_mode).

    Focus mode (review of one exam only) applies on the day of an afternoon/evening exam
    (final review that morning) and on the eve of an exam. Otherwise all upcoming exams compete.
    """
    same_day_late = sorted(
        (e for e in exams if days_until(e, day, cfg) == 0 and is_late_exam(e, cfg)),
        key=lambda e: e.starts_at,
    )
    if same_day_late:
        return same_day_late[:1], True
    upcoming = sorted((e for e in exams if days_until(e, day, cfg) > 0), key=lambda e: e.starts_at)
    eve = [e for e in upcoming if days_until(e, day, cfg) == 1]
    if eve and cfg.eve_review_only:
        return eve[:1], True
    # Agreed pauses ("don't touch Networks until Dec 3") never override an exam's eve.
    active = [e for e in upcoming if cfg.paused_until.get(e.id, date.min) <= day]
    return active, False


def _pick_kind(state: _TopicState, exam: ExamInfo, day: date, cfg: PlanConfig) -> BlockKind:
    phase = phase_for(exam, day, cfg)
    review_due = state.next_review is not None and state.next_review <= day
    if phase in ("review", "exam_day") or (review_due and state.remaining <= 0):
        return "review"
    if review_due and state.studied and state.remaining > 0 and state.virtual_level() >= 2:
        return "review"
    level = state.virtual_level()
    if phase == "learn":
        return "learn" if level < 1.5 else "practice"
    return "learn" if level < 1 else "practice"


def _score(
    state: _TopicState, exam: ExamInfo, day: date, cfg: PlanConfig, focus: bool
) -> float | None:
    """Score for placing this topic now, or None if it is not allowed."""
    if state.blocks_today >= cfg.max_blocks_per_topic_per_day:
        return None
    if focus and cfg.no_new_topics_eve and not state.studied:
        return None  # no new topics the day before an exam
    review_due = state.next_review is not None and state.next_review <= day
    # Score with the *virtual* review date, so reviews the planner scheduled itself count the
    # same as reviews recorded in the DB (keeps replans consistent with the original plan).
    virtual = replace(state.topic, next_review_at=_review_dt(state.next_review, cfg))
    base = priority(virtual, exam, day, cfg, level=state.virtual_level()).total
    if state.remaining <= 0 and not review_due:
        return base * EXTRA_PRACTICE_FACTOR
    return base + (REVIEW_DUE_BONUS if review_due else 0.0)


def _review_dt(day: date | None, cfg: PlanConfig) -> datetime | None:
    return local_to_utc(day, time(9, 0), cfg.zone) if day else None


def _apply(state: _TopicState, kind: BlockKind, day: date, cfg: PlanConfig) -> None:
    intervals = cfg.review_intervals_days
    if kind in ("learn", "practice"):
        state.remaining = max(0.0, state.remaining - cfg.block_min)
    first_today = state.blocks_today == 0
    state.blocks_today += 1
    state.studied = True
    if first_today:
        step = min(state.review_step, len(intervals) - 1)
        state.next_review = day + timedelta(days=intervals[step])
        state.review_step += 1


def _best_topic(
    states: dict[int, _TopicState],
    exams: Sequence[ExamInfo],
    day: date,
    cfg: PlanConfig,
    focus: bool,
    preferred: tuple[int | None, frozenset[int]] = (None, frozenset()),
) -> tuple[_TopicState, ExamInfo] | None:
    """Highest-scoring allowed topic. `preferred` = (topic the current plan has in this slot,
    topics the current plan has on this day) — small bonuses that keep replans stable."""
    in_slot, in_day = preferred
    best: tuple[float, int, _TopicState, ExamInfo] | None = None
    for exam in exams:
        for st in states.values():
            if st.topic.exam_id != exam.id:
                continue
            score = _score(st, exam, day, cfg, focus)
            if score is None:
                continue
            if st.topic.id == in_slot:
                score += STABILITY_SLOT_BONUS
            elif st.topic.id in in_day:
                score += STABILITY_DAY_BONUS
            key = (score, -st.topic.id)
            if best is None or key > (best[0], best[1]):
                best = (score, -st.topic.id, st, exam)
    return (best[2], best[3]) if best else None


def _consecutive_pair(slots: list[Slot], cfg: PlanConfig) -> int | None:
    for i in range(len(slots) - 1):
        if slots[i + 1].start - slots[i].end <= timedelta(minutes=cfg.break_min):
            return i
    return None


def _place_mock(
    slots: list[Slot], eligible: list[ExamInfo], day: date, cfg: PlanConfig
) -> list[PlannedBlock]:
    candidates = [e for e in eligible if days_until(e, day, cfg) in cfg.mock_days_before]
    if not candidates:
        return []
    exam = min(candidates, key=lambda e: e.starts_at)
    i = _consecutive_pair(slots, cfg)
    if i is None:
        return []
    pair = [slots.pop(i), slots.pop(i)]
    return [PlannedBlock(s.start, s.end, exam.id, None, "mock") for s in pair]


def _plan_day(
    day: date,
    slots: list[Slot],
    exams: Sequence[ExamInfo],
    states: dict[int, _TopicState],
    last_block_day: dict[int, date],
    cfg: PlanConfig,
    anchor: Mapping[datetime, int | None] | None = None,
) -> list[PlannedBlock]:
    for st in states.values():
        st.blocks_today = 0
    eligible, focus = _eligible_exams(day, exams, cfg)
    if not eligible or not slots:
        return []
    free = list(slots)
    blocks = (
        []
        if focus or any(exam_local_date(e, cfg) == day for e in exams)
        else _place_mock(free, eligible, day, cfg)
    )

    queue: list[list[ExamInfo]] = []
    if not focus:
        yesterday = day - timedelta(days=1)
        cold = [e for e in eligible if last_block_day.get(e.id, date.min) < yesterday]
        queue.extend([e] for e in cold if not any(b.exam_id == e.id for b in blocks))
    day_anchor = frozenset(
        t for s, t in (anchor or {}).items() if s.astimezone(cfg.zone).date() == day
    )
    while free:
        pool = queue.pop(0) if queue else eligible
        slot = free[0]
        preferred = ((anchor or {}).get(slot.start), day_anchor)
        choice = _best_topic(states, pool, day, cfg, focus, preferred)
        if choice is None:
            if pool is eligible:
                break
            continue
        st, exam = choice
        kind = _pick_kind(st, exam, day, cfg)
        free.pop(0)
        _apply(st, kind, day, cfg)
        blocks.append(PlannedBlock(slot.start, slot.end, exam.id, st.topic.id, kind))
    for b in blocks:
        last_block_day[b.exam_id] = day
    return sorted(blocks, key=lambda b: b.start)


def build_plan(
    exams: Sequence[ExamInfo],
    topics: Sequence[TopicInfo],
    cfg: PlanConfig,
    *,
    busy: Sequence[Interval],
    now: datetime,
    anchor: Sequence[PlannedBlock] = (),
    history: Mapping[int, date] | None = None,
) -> PlanResult:
    """Plan every remaining study block from `now` until the last exam.

    `anchor` is the current plan: when scores are close, the allocator keeps its choices, so a
    nightly replan after one missed block does not reshuffle the whole schedule. `history`
    (exam_id → last day it had a block) seeds the keep-warm rule across replans."""
    anchor_map = {b.start: b.topic_id for b in anchor}
    if not exams:
        return PlanResult((), Feasibility(True, 0.0))
    today = now.astimezone(cfg.zone).date()
    last_day = max(exam_local_date(e, cfg) for e in exams)
    feasibility = check_feasibility(exams, topics, cfg, today)
    states = _init_states(topics, cfg)
    last_block_day: dict[int, date] = dict(history or {})
    blocks: list[PlannedBlock] = []
    day = today
    while day <= last_day:
        slots = day_slots(day, cfg, exams, busy, not_before=now if day == today else None)
        blocks.extend(_plan_day(day, slots, exams, states, last_block_day, cfg, anchor_map))
        day += timedelta(days=1)
    leftover = sum(st.remaining for st in states.values()) / 60
    return PlanResult(tuple(blocks), feasibility, round(leftover, 2))
