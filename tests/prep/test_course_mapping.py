from agent.canvas.api import CanvasCourse
from agent.prep.course_mapping import suggest_mapping


def course(cid: int, name: str, code: str | None = None) -> CanvasCourse:
    return CanvasCourse(
        id=cid,
        name=name,
        course_code=code,
        current_score=None,
        current_grade=None,
        apply_group_weights=False,
    )


COURSES = [
    course(101, "CS3201 Computer Networks (Sem A 2026/27)", "CS3201"),
    course(102, "CS4335 Design and Analysis of Algorithms", "CS4335"),
    course(103, "MA2172 Applied Linear Algebra", "MA2172"),
    course(104, "CS4187 Computer Vision for Interactivity", "CS4187"),
    course(105, "CS3402 Database Systems", "CS3402"),
    course(106, "GE2410 Chinese Culture and Society", "GE2410"),
]

EXAMS = [
    "Computer Networks",
    "Design and Analysis of Algorithms",
    "Algebra",
    "Computer Vision for Interactivity (CS4187)",
]


def test_maps_all_four_finals_to_the_right_courses() -> None:
    result = {s.exam_title: s.course_id for s in suggest_mapping(EXAMS, COURSES)}
    assert result == {
        "Computer Networks": 101,
        "Design and Analysis of Algorithms": 102,
        "Algebra": 103,
        "Computer Vision for Interactivity (CS4187)": 104,
    }


def test_course_code_in_exam_title_is_an_exact_match() -> None:
    [s] = suggest_mapping(["Computer Vision for Interactivity (CS4187)"], COURSES)
    assert s.course_id == 104
    assert s.score == 1.0


def test_each_course_is_used_at_most_once() -> None:
    courses = [course(1, "Algebra I"), course(2, "Algebra II")]
    suggestions = suggest_mapping(["Algebra", "Algebra"], courses)
    ids = [s.course_id for s in suggestions if s.course_id is not None]
    assert len(ids) == len(set(ids))


def test_no_match_below_threshold_returns_none_with_alternatives() -> None:
    [s] = suggest_mapping(["Quantum Basket Weaving"], COURSES)
    assert s.course_id is None
    assert len(s.alternatives) > 0


def test_close_runner_up_is_flagged_ambiguous() -> None:
    courses = [course(1, "Linear Algebra"), course(2, "Abstract Algebra")]
    [s] = suggest_mapping(["Algebra"], courses)
    assert s.ambiguous
