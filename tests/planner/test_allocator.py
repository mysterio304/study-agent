"""Joint multi-exam plan: shape, non-negotiables, determinism."""

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta

import pytest

from agent.planner.allocator import build_plan
from agent.planner.validators import validate_plan
from tests.planner.conftest import ALGEBRA, ALGO, CV, HKT, NETWORKS, at

START = date(2026, 11, 19)
NOW = at(START, 8, 0)


@pytest.fixture
def plan(cfg, exams, topics):
    return build_plan(exams, topics, cfg, busy=[], now=NOW)


def by_day(blocks):
    days = defaultdict(list)
    for b in blocks:
        days[b.start.astimezone(HKT).date()].append(b)
    return days


def test_plan_has_no_rule_violations(plan, cfg, exams, topics) -> None:
    assert plan.blocks
    assert validate_plan(plan.blocks, exams, topics, cfg, busy=[]) == []


def test_day_before_each_exam_is_review_of_that_exam_only(plan, cfg, exams) -> None:
    days = by_day(plan.blocks)
    for exam in exams:
        eve = exam.starts_at.astimezone(HKT).date() - timedelta(days=1)
        blocks = days[eve]
        exam_blocks_eve = [b for b in blocks if b.exam_id == exam.id]
        assert any(b.kind == "review" for b in exam_blocks_eve), f"no review before {exam.title}"
        # Dec 8 is both the eve of Algorithms and the Networks exam day: only Algorithms.
        assert {b.exam_id for b in blocks} == {exam.id}
        assert {b.kind for b in blocks} <= {"review", "mock"}


def test_no_blocks_for_an_exam_after_it_happened(plan, exams) -> None:
    starts = {e.id: e.starts_at for e in exams}
    assert all(b.end <= starts[b.exam_id] for b in plan.blocks)


def test_networks_and_algorithms_dominate_early_learning(plan) -> None:
    early = [b for b in plan.blocks if b.start < at(date(2026, 12, 2), 0)]
    counts = Counter(b.exam_id for b in early)
    assert counts[NETWORKS] + counts[ALGO] > 0.6 * len(early)
    assert counts[ALGEBRA] > 0 and counts[CV] > 0  # kept warm


def test_every_exam_kept_warm_at_least_every_two_days(plan, exams) -> None:
    """On non-focus days every upcoming exam appears at least once per two days.
    (The eve of an exam is reserved for that exam, so focus days are excluded.)"""
    days = by_day(plan.blocks)
    eves = {e.starts_at.astimezone(HKT).date() - timedelta(days=1) for e in exams}
    for exam in exams:
        exam_day = exam.starts_at.astimezone(HKT).date()
        d = START
        while d + timedelta(days=1) < exam_day - timedelta(days=1):
            pair_days = [d, d + timedelta(days=1)]
            if not eves & set(pair_days):
                pair = days[pair_days[0]] + days[pair_days[1]]
                assert any(b.exam_id == exam.id for b in pair), f"{exam.title} cold on {d}"
            d += timedelta(days=1)


def test_topic_at_most_twice_a_day(plan, cfg) -> None:
    for blocks in by_day(plan.blocks).values():
        counts = Counter(b.topic_id for b in blocks if b.topic_id is not None)
        assert max(counts.values(), default=0) <= cfg.max_blocks_per_topic_per_day


def test_mock_exams_scheduled_before_networks(plan) -> None:
    mocks = [b for b in plan.blocks if b.kind == "mock" and b.exam_id == NETWORKS]
    assert {b.start.astimezone(HKT).date() for b in mocks} & {date(2026, 12, 4), date(2026, 12, 6)}


def test_plan_is_deterministic(cfg, exams, topics) -> None:
    a = build_plan(exams, topics, cfg, busy=[], now=NOW)
    b = build_plan(exams, topics, cfg, busy=[], now=NOW)
    assert a.blocks == b.blocks


def test_untouched_topics_never_studied_the_day_before_their_exam(cfg, exams, topics) -> None:
    fresh = [replace(t, level=0) for t in topics]
    plan = build_plan(exams, fresh, cfg, busy=[], now=at(date(2026, 12, 6), 8))
    level0 = {t.id for t in fresh}
    days = by_day(plan.blocks)
    for exam in exams:
        eve = exam.starts_at.astimezone(HKT).date() - timedelta(days=1)
        for b in days[eve]:
            # Only topics already studied earlier in this plan may appear (as review).
            if b.topic_id in level0:
                assert b.kind == "review"
                earlier = [x for x in plan.blocks if x.topic_id == b.topic_id and x.start < b.start]
                assert earlier, "a new topic was introduced the day before an exam"


def test_infeasible_plan_reports_shortfall_and_drop_candidates(cfg, exams, topics) -> None:
    huge = [replace(t, effort_hours=40.0, level=0) for t in topics]
    plan = build_plan(exams, huge, cfg, busy=[], now=NOW)
    assert not plan.feasibility.ok
    assert plan.feasibility.shortfall_hours > 0
    assert plan.feasibility.drop_candidates
    assert validate_plan(plan.blocks, exams, huge, cfg, busy=[]) == []  # still never overloads


def test_feasible_plan_ok(plan) -> None:
    assert plan.feasibility.ok
    assert plan.feasibility.shortfall_hours == 0


def test_final_review_on_the_morning_of_late_exams(plan, exams) -> None:
    days = by_day(plan.blocks)
    for exam in exams[1:]:  # Algorithms 18:30, Algebra 18:30, CV 14:00
        day = exam.starts_at.astimezone(HKT).date()
        same_day = [b for b in days[day] if b.end <= exam.starts_at]
        assert same_day, f"no final review before {exam.title}"
        assert {b.exam_id for b in same_day} == {exam.id}
        assert {b.kind for b in same_day} == {"review"}


def test_paused_exam_and_day_cap_overrides(cfg, exams, topics) -> None:
    from types import MappingProxyType

    tuned = replace(
        cfg,
        paused_until=MappingProxyType({NETWORKS: date(2026, 11, 25)}),
        day_caps=MappingProxyType({date(2026, 11, 20): 2.0}),
    )
    plan = build_plan(exams, topics, tuned, busy=[], now=NOW)
    days = by_day(plan.blocks)
    assert not any(
        b.exam_id == NETWORKS for b in plan.blocks if b.start < at(date(2026, 11, 25), 0)
    )
    assert any(b.exam_id == NETWORKS for b in days[date(2026, 11, 25)])
    assert len(days[date(2026, 11, 20)]) == 2


def test_non_negotiables_can_be_disabled_explicitly(cfg, exams, topics) -> None:
    relaxed = replace(cfg, eve_review_only=False, protected_sleep=False)
    plan = build_plan(exams, topics, relaxed, busy=[], now=NOW)
    dec7 = by_day(plan.blocks)[date(2026, 12, 7)]
    assert {b.exam_id for b in dec7} != {NETWORKS}  # eve no longer exclusive
    assert validate_plan(plan.blocks, exams, topics, relaxed, busy=[]) == []


def _simulate_day_done(topics, blocks, day, cfg):
    """Apply 'the student did exactly the planned blocks of `day`' to topic state."""
    from datetime import time as dtime

    from agent.core.timeutil import local_to_utc

    by_id = {t.id: t for t in topics}
    for b in sorted(blocks, key=lambda x: x.start):
        if b.start.astimezone(HKT).date() != day or b.topic_id is None:
            continue
        t = by_id[b.topic_id]
        first_today = t.last_studied_at is None or t.last_studied_at.astimezone(HKT).date() != day
        step = min(t.review_step, len(cfg.review_intervals_days) - 1)
        nxt = local_to_utc(day + timedelta(days=cfg.review_intervals_days[step]), dtime(9), HKT)
        by_id[t.id] = replace(
            t,
            progress_min=t.progress_min + (50 if b.kind in ("learn", "practice") else 0),
            last_studied_at=b.end,
            next_review_at=nxt if first_today else t.next_review_at,
            review_step=t.review_step + (1 if first_today else 0),
        )
    return list(by_id.values())


def test_replan_after_following_the_plan_barely_changes_it(cfg, exams, topics) -> None:
    from agent.planner.render import changed_blocks

    first = build_plan(exams, topics, cfg, busy=[], now=NOW)
    done = _simulate_day_done(topics, first.blocks, START, cfg)
    tomorrow = at(date(2026, 11, 20), 8, 0)
    remaining = [b for b in first.blocks if b.start > tomorrow]
    history = {e.id: START for e in exams}
    anchored = build_plan(
        exams, done, cfg, busy=[], now=tomorrow, anchor=remaining, history=history
    )
    free = build_plan(exams, done, cfg, busy=[], now=tomorrow, history=history)
    assert changed_blocks(remaining, anchored.blocks) <= 0.15 * len(anchored.blocks)
    assert changed_blocks(remaining, anchored.blocks) <= changed_blocks(remaining, free.blocks)
