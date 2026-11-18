"""Daily slot generation: where study blocks *may* go, and how many a day can hold."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from agent.core.timeutil import local_to_utc, parse_hhmm
from agent.planner.model import ExamInfo, Interval, PlanConfig, Slot
from agent.planner.priority import exam_local_date, is_late_exam


def _first_exam_date(exams: Sequence[ExamInfo], cfg: PlanConfig) -> date | None:
    return min((exam_local_date(e, cfg) for e in exams), default=None)


def _is_exam_day(day: date, exams: Sequence[ExamInfo], cfg: PlanConfig) -> bool:
    return any(exam_local_date(e, cfg) == day for e in exams)


def day_cap_hours(day: date, cfg: PlanConfig, exams: Sequence[ExamInfo]) -> float:
    """Study-time cap for a local day (0 once every exam is over)."""
    if day in cfg.day_caps:
        return float(cfg.day_caps[day])
    remaining = [e for e in exams if exam_local_date(e, cfg) >= day]
    first = _first_exam_date(exams, cfg)
    if not remaining or first is None:
        return 0.0
    if _is_exam_day(day, exams, cfg):
        has_later = any(exam_local_date(e, cfg) > day for e in exams)
        late_today = any(exam_local_date(e, cfg) == day and is_late_exam(e, cfg) for e in exams)
        return cfg.exam_day_cap_hours if has_later or late_today else 0.0
    if day > first:
        cap = cfg.between_exams_cap_hours
    elif (first - day).days <= 7:
        cap = cfg.final_week_cap_hours
    else:
        cap = cfg.cap_hours
    nearest = min((exam_local_date(e, cfg) - day).days for e in remaining)
    if day.weekday() == cfg.lighter_weekday and nearest > cfg.lighter_skip_if_exam_within_days:
        cap *= cfg.lighter_factor
    return cap


def day_capacity_blocks(day: date, cfg: PlanConfig, exams: Sequence[ExamInfo]) -> int:
    return math.floor(day_cap_hours(day, cfg, exams) * 60 / cfg.block_min + 1e-9)


def forbidden_intervals(
    day: date, cfg: PlanConfig, exams: Sequence[ExamInfo], busy: Sequence[Interval]
) -> list[Interval]:
    """Exams ± buffers, user busy time, and protected sleep before morning exams."""
    blocked = list(busy)
    for e in exams:
        end = e.starts_at + timedelta(minutes=e.duration_min)
        if cfg.no_blocks_during_exams:
            blocked.append(
                Interval(
                    e.starts_at - timedelta(minutes=cfg.exam_buffer_before_min),
                    end + timedelta(minutes=cfg.exam_buffer_after_min),
                )
            )
        local_start = e.starts_at.astimezone(cfg.zone)
        is_eve = local_start.date() - day == timedelta(1)
        if cfg.protected_sleep and local_start.hour < cfg.morning_exam_before_hour and is_eve:
            cutoff = local_to_utc(day, parse_hhmm(cfg.protected_sleep_last_block_end), cfg.zone)
            blocked.append(Interval(cutoff, e.starts_at))
    return blocked


def _window(day: date, cfg: PlanConfig) -> Interval:
    start = local_to_utc(day, cfg.study_window.start, cfg.zone)
    end_day = day + timedelta(days=1) if cfg.study_window.wraps_midnight else day
    end = local_to_utc(end_day, cfg.study_window.end, cfg.zone)
    return Interval(start, end)


def _in_quiet_hours(iv: Interval, cfg: PlanConfig) -> bool:
    step = timedelta(minutes=10)
    t = iv.start
    while t < iv.end:
        if cfg.quiet_hours.contains(t.astimezone(cfg.zone).time()):
            return True
        t += step
    return False


def day_slots(
    day: date,
    cfg: PlanConfig,
    exams: Sequence[ExamInfo],
    busy: Sequence[Interval],
    *,
    not_before: datetime | None = None,
) -> list[Slot]:
    """Block positions for `day`: the 50/10 rhythm with a long break every N blocks, skipping
    forbidden time, limited to the day's capacity. An interruption resets the rhythm."""
    capacity = day_capacity_blocks(day, cfg, exams)
    if capacity <= 0:
        return []
    window = _window(day, cfg)
    blocked = forbidden_intervals(day, cfg, exams, busy)
    block, brk = timedelta(minutes=cfg.block_min), timedelta(minutes=cfg.break_min)
    long_brk = timedelta(minutes=cfg.long_break_min)

    slots: list[Slot] = []
    cursor, streak = window.start, 0
    while cursor + block <= window.end and len(slots) < capacity:
        candidate = Interval(cursor, cursor + block)
        clash = next((b for b in blocked if b.overlaps(candidate)), None)
        if clash is not None:
            cursor, streak = max(clash.end, cursor + timedelta(minutes=10)), 0
            continue
        if (not_before and candidate.start < not_before) or _in_quiet_hours(candidate, cfg):
            cursor += timedelta(minutes=10)
            continue
        slots.append(Slot(day, candidate.start, candidate.end))
        streak += 1
        cursor = candidate.end + (long_brk if streak % cfg.blocks_before_long_break == 0 else brk)
    return slots
