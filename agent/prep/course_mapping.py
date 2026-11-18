"""Fuzzy mapping of the seeded finals to Canvas courses.

Suggestions are only *suggestions*: the /start wizard shows them and you confirm with a button
(`Exam.mapping_confirmed`). Scoring, in order of trust:

1. a course code in the exam title (e.g. "(CS4187)") that matches the course → score 1.0;
2. otherwise max(token containment, character similarity) on normalised names, where
   containment = share of the exam's meaningful words that appear in the course name.

Assignment is greedy by score so each course is used at most once.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from agent.canvas.api import CanvasCourse

MATCH_THRESHOLD = 0.5
AMBIGUITY_MARGIN = 0.1

_CODE_RE = re.compile(r"\b([A-Z]{2,4})\s?(\d{4})\b")
_STOPWORDS = frozenset({"and", "for", "of", "the", "in", "to", "a", "an", "sem", "semester"})
_NOISE_RE = re.compile(r"\([^)]*\)|\b(sem|semester)\s*[ab]\b|\d{4}/\d{2}")


@dataclass(frozen=True, slots=True)
class MappingSuggestion:
    exam_title: str
    course_id: int | None
    course_name: str | None
    score: float
    ambiguous: bool = False
    alternatives: tuple[tuple[int, str, float], ...] = field(default=())


def _codes(text: str) -> set[str]:
    return {a + b for a, b in _CODE_RE.findall(text.upper())}


def _normalise(text: str) -> str:
    text = _NOISE_RE.sub(" ", text.lower())
    text = _CODE_RE.sub(" ", text.upper()).lower()
    return " ".join(re.findall(r"[a-z]+", text))


def _tokens(text: str) -> set[str]:
    return {t for t in _normalise(text).split() if t not in _STOPWORDS}


def match_score(exam_title: str, course: CanvasCourse) -> float:
    course_text = f"{course.course_code or ''} {course.name}"
    exam_codes = _codes(exam_title)
    if exam_codes and exam_codes & _codes(course_text):
        return 1.0
    exam_tokens, course_tokens = _tokens(exam_title), _tokens(course.name)
    if not exam_tokens or not course_tokens:
        return 0.0
    containment = len(exam_tokens & course_tokens) / len(exam_tokens)
    similarity = SequenceMatcher(None, _normalise(exam_title), _normalise(course.name)).ratio()
    # Slight preference for courses without extra words, so "Algebra" prefers shorter names.
    precision = len(exam_tokens & course_tokens) / len(course_tokens)
    return round(max(containment * (0.9 + 0.1 * precision), similarity), 4)


def suggest_mapping(
    exam_titles: Sequence[str], courses: Sequence[CanvasCourse]
) -> list[MappingSuggestion]:
    scored = {
        i: sorted(((match_score(t, c), c) for c in courses), key=lambda x: x[0], reverse=True)
        for i, t in enumerate(exam_titles)
    }
    pairs = sorted(
        ((score, i, c) for i, ranked in scored.items() for score, c in ranked),
        key=lambda x: x[0],
        reverse=True,
    )
    chosen: dict[int, tuple[float, CanvasCourse]] = {}
    used: set[int] = set()
    for score, i, c in pairs:
        if score < MATCH_THRESHOLD or i in chosen or c.id in used:
            continue
        chosen[i] = (score, c)
        used.add(c.id)

    result: list[MappingSuggestion] = []
    for i, title in enumerate(exam_titles):
        ranked = scored[i]
        alternatives = tuple((c.id, c.name, s) for s, c in ranked[:3])
        if i not in chosen:
            result.append(MappingSuggestion(title, None, None, 0.0, True, alternatives))
            continue
        score, c = chosen[i]
        runner_up = next((s for s, other in ranked if other.id != c.id), 0.0)
        ambiguous = score < 1.0 and score - runner_up < AMBIGUITY_MARGIN
        result.append(MappingSuggestion(title, c.id, c.name, score, ambiguous, alternatives))
    return result
