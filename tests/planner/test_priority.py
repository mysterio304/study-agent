from dataclasses import replace
from datetime import date, timedelta

import pytest

from agent.planner.model import ExamInfo
from agent.planner.priority import forgetting, needed_final_pct, phase_for, priority, stakes
from tests.planner.conftest import at


def test_needed_final_pct_formula() -> None:
    # current 78.4 on 30% coursework, target A- (80): (80 - 78.4*0.3)/0.7 = 80.69
    assert needed_final_pct(78.4, 0.7, 80) == pytest.approx(80.69, abs=0.01)
    assert needed_final_pct(None, 0.7, 80) is None
    assert needed_final_pct(80, None, 80) is None


def test_stakes_unknown_weight_defaults_and_bounds(exams: list[ExamInfo], cfg) -> None:
    networks, _, algebra, _ = exams
    s_net = stakes(networks, cfg)
    assert 0 < s_net <= 1
    s_alg = stakes(algebra, cfg)  # weight unknown → default 0.5
    assert s_alg == pytest.approx(0.5 * max(0.3, min(1.0, (80 - 71.5 * 0.5) / 0.5 / 100)))
    secured = replace(networks, current_score=100.0, final_weight=0.1)
    assert stakes(secured, cfg) == pytest.approx(0.1 * 0.3)  # floor at 0.3


@pytest.mark.parametrize(
    ("days_before", "phase"),
    [
        (20, "learn"),
        (8, "learn"),
        (7, "practice"),
        (2, "practice"),
        (1, "review"),
        (0, "exam_day"),
        (-1, "done"),
    ],
)
def test_phase_for(exams, cfg, days_before: int, phase: str) -> None:
    networks = exams[0]
    day = date(2026, 12, 8) - timedelta(days=days_before)
    assert phase_for(networks, day, cfg) == phase


def test_priority_terms_and_ordering(exams, topics, cfg) -> None:
    today = date(2026, 11, 19)
    by_id = {e.id: e for e in exams}
    net_core = next(t for t in topics if t.id == 100)  # importance 3, level 1
    cv_side = next(t for t in topics if t.id == 402)  # importance 2, level 2, exam far away
    p1 = priority(net_core, by_id[1], today, cfg)
    p2 = priority(cv_side, by_id[4], today, cfg)
    assert p1.total > p2.total
    assert set(p1.terms) == {"stakes", "gap", "importance", "urgency", "forgetting"}
    assert all(0 <= v <= 1 for v in p1.terms.values())
    assert p1.total == pytest.approx(sum(cfg.weights[k] * v for k, v in p1.terms.items()))


def test_forgetting_term_rises_when_review_overdue(exams, topics, cfg) -> None:
    today = date(2026, 11, 25)
    t = replace(topics[0], next_review_at=None)
    assert forgetting(t, today, cfg) == 0
    due = replace(t, next_review_at=at(date(2026, 11, 22), 9))
    assert forgetting(due, today, cfg) == pytest.approx(1.0)  # 3 days overdue → min(1, .5+1)
