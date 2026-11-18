"""Grade calculator (SPEC §6): the final-exam score needed for each target letter.

    course% = C · (1 − w) + x · w      →      x = (target − C · (1 − w)) / w

w (final weight) comes from Canvas when the final exists there — a group named like
"Final/Exam" (weighted courses) or an ungraded "final exam" assignment (points-based) — and
otherwise from the exam's known weight. C (coursework %) is computed in two scenarios:
`if_current` — ungraded coursework ends at your current average; `if_zero` — it scores 0.
Excused and omitted assignments are ignored. Drop rules, curves and rounding are NOT modelled,
so results are estimates (the UI says so).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# "Final", "Final Exam(ination)", or a bare "Exam"/"Examination" — but not "Exam 1" or "Midterm Exam".
FINAL_RE = re.compile(r"\bfinal\b|^\s*exam(ination)?s?\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class GroupIn:
    id: int
    name: str
    weight: float | None  # Canvas group_weight (percent); None/0 in points-based courses


@dataclass(frozen=True, slots=True)
class AssignmentIn:
    group_id: int | None
    points: float | None
    score: float | None
    name: str = ""
    excused: bool = False
    omit: bool = False


@dataclass(frozen=True, slots=True)
class Need:
    cutoff: float
    if_current: float
    if_zero: float


@dataclass(frozen=True, slots=True)
class NeedResult:
    final_weight: float | None
    weight_source: str  # canvas | exam | unknown
    coursework_avg: float | None
    coursework_zero: float | None
    need: dict[str, Need] = field(default_factory=dict)


def _counted(items: Sequence[AssignmentIn]) -> list[AssignmentIn]:
    return [a for a in items if not a.excused and not a.omit and (a.points or 0) > 0]


def _pct(score: float, points: float) -> float | None:
    return 100 * score / points if points > 0 else None


def _weighted(
    groups: Sequence[GroupIn], items: Sequence[AssignmentIn]
) -> tuple[float | None, float | None, float | None]:
    finals = {
        g.id
        for g in groups
        if FINAL_RE.search(g.name)
        and not any(a.group_id == g.id and a.score is not None for a in items)
    }
    total_w = sum(g.weight or 0 for g in groups)
    if total_w <= 0:
        return None, None, None
    w_final = sum(g.weight or 0 for g in groups if g.id in finals) / total_w if finals else None
    num_avg = den_avg = num_zero = den_zero = 0.0
    for g in groups:
        if g.id in finals or not g.weight:
            continue
        mine = [a for a in _counted(items) if a.group_id == g.id]
        graded = [a for a in mine if a.score is not None]
        got = sum(a.score or 0 for a in graded)
        graded_pts = sum(a.points or 0 for a in graded)
        all_pts = sum(a.points or 0 for a in mine)
        if graded_pts > 0:
            num_avg += g.weight * got / graded_pts
            den_avg += g.weight
        if all_pts > 0:
            num_zero += g.weight * got / all_pts
            den_zero += g.weight
    avg = 100 * num_avg / den_avg if den_avg else None
    zero = 100 * num_zero / den_zero if den_zero else None
    return w_final, avg, zero


def _points(items: Sequence[AssignmentIn]) -> tuple[float | None, float | None, float | None]:
    counted = _counted(items)
    finals = [a for a in counted if a.score is None and FINAL_RE.search(a.name)]
    rest = [a for a in counted if a not in finals]
    total = sum(a.points or 0 for a in counted)
    w_final = sum(a.points or 0 for a in finals) / total if finals and total else None
    graded = [a for a in rest if a.score is not None]
    got = sum(a.score or 0 for a in graded)
    avg = _pct(got, sum(a.points or 0 for a in graded))
    zero = _pct(got, sum(a.points or 0 for a in rest))
    return w_final, avg, zero


def needed_on_final(
    groups: Sequence[GroupIn],
    items: Sequence[AssignmentIn],
    *,
    weighted: bool,
    exam_final_weight: float | None,
    canvas_current: float | None,
    cutoffs: Mapping[str, float],
) -> NeedResult:
    w_canvas, avg, zero = _weighted(groups, items) if weighted else _points(items)
    if w_canvas is None and weighted and avg is None:  # weights all zero → treat as points
        w_canvas, avg, zero = _points(items)
    w, source = (
        (w_canvas, "canvas")
        if w_canvas
        else (exam_final_weight, "exam" if exam_final_weight else "unknown")
    )
    avg = avg if avg is not None else canvas_current
    zero = zero if zero is not None else avg
    if not w or avg is None:
        return NeedResult(w, source, avg, zero)
    need = {
        letter: Need(
            cut,
            (cut - avg * (1 - w)) / w,
            (cut - (zero if zero is not None else avg) * (1 - w)) / w,
        )
        for letter, cut in cutoffs.items()
    }
    return NeedResult(w, source, avg, zero, need)
