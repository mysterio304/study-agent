import pytest

from agent.grades.calculator import AssignmentIn, GroupIn, needed_on_final

CUTS = {"A+": 90, "A": 85, "A-": 80, "B+": 75}


def test_weighted_course_with_final_group_in_canvas() -> None:
    groups = [
        GroupIn(1, "Assignments", 30),
        GroupIn(2, "Midterm", 20),
        GroupIn(3, "Final Exam", 50),
    ]
    items = [
        AssignmentIn(1, 10, 9),
        AssignmentIn(1, 10, 8),
        AssignmentIn(1, 10, None),  # one ungraded
        AssignmentIn(2, 100, 70),
        AssignmentIn(3, 100, None),
    ]
    r = needed_on_final(
        groups, items, weighted=True, exam_final_weight=None, canvas_current=None, cutoffs=CUTS
    )
    assert r.final_weight == pytest.approx(0.5) and r.weight_source == "canvas"
    # coursework at current average: (30*0.85 + 20*0.70) / 50 = 0.79
    assert r.coursework_avg == pytest.approx(79.0)
    # if the ungraded assignment scores 0: (30*17/30 + 20*0.7)/50 = 0.62
    assert r.coursework_zero == pytest.approx(62.0)
    a_minus = r.need["A-"]
    assert a_minus.if_current == pytest.approx(81.0)  # (80 - 79*0.5)/0.5
    assert a_minus.if_zero == pytest.approx(98.0)


def test_points_based_course_with_final_assignment() -> None:
    groups = [GroupIn(1, "All", None)]
    items = [
        AssignmentIn(1, 50, 45),
        AssignmentIn(1, 50, 40),
        AssignmentIn(1, 100, None, name="Final exam"),
    ]
    r = needed_on_final(
        groups, items, weighted=False, exam_final_weight=None, canvas_current=None, cutoffs=CUTS
    )
    assert r.final_weight == pytest.approx(0.5)
    assert r.coursework_avg == pytest.approx(85.0)
    assert r.need["A"].if_current == pytest.approx(85.0)


def test_final_not_in_canvas_uses_exam_weight() -> None:
    groups = [GroupIn(1, "Coursework", 100)]
    items = [AssignmentIn(1, 100, 78.4)]
    r = needed_on_final(
        groups, items, weighted=True, exam_final_weight=0.7, canvas_current=None, cutoffs=CUTS
    )
    assert r.weight_source == "exam"
    assert r.need["A-"].if_current == pytest.approx((80 - 78.4 * 0.3) / 0.7)


def test_unknown_weight_and_nothing_graded() -> None:
    r = needed_on_final(
        [GroupIn(1, "HW", 100)],
        [AssignmentIn(1, 10, None)],
        weighted=True,
        exam_final_weight=None,
        canvas_current=None,
        cutoffs=CUTS,
    )
    assert r.final_weight is None and r.need == {}
    r2 = needed_on_final(
        [GroupIn(1, "HW", 100)],
        [AssignmentIn(1, 10, None)],
        weighted=True,
        exam_final_weight=0.6,
        canvas_current=72.0,
        cutoffs=CUTS,
    )
    assert r2.coursework_avg == 72.0  # falls back to Canvas' current score


def test_excused_and_omitted_work_is_ignored() -> None:
    groups = [GroupIn(1, "HW", 40), GroupIn(2, "Final", 60)]
    items = [
        AssignmentIn(1, 10, 10),
        AssignmentIn(1, 10, 0, excused=True),
        AssignmentIn(1, 10, 0, omit=True),
        AssignmentIn(2, 100, None),
    ]
    r = needed_on_final(
        groups, items, weighted=True, exam_final_weight=None, canvas_current=None, cutoffs=CUTS
    )
    assert r.coursework_avg == pytest.approx(100.0)


def test_impossible_and_secured_flags() -> None:
    groups = [GroupIn(1, "HW", 50), GroupIn(2, "Final", 50)]
    low = needed_on_final(
        groups,
        [AssignmentIn(1, 100, 30), AssignmentIn(2, 100, None)],
        weighted=True,
        exam_final_weight=None,
        canvas_current=None,
        cutoffs=CUTS,
    )
    assert low.need["A+"].if_current > 100
    high = needed_on_final(
        groups,
        [AssignmentIn(1, 100, 100), AssignmentIn(2, 100, None)],
        weighted=True,
        exam_final_weight=None,
        canvas_current=None,
        cutoffs={"D": 45},
    )
    assert high.need["D"].if_current <= 0
