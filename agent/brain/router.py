"""Rule-based model routing (SPEC §9a.2) — a pure function, no LLM call.

Haiku handles everything by default. Sonnet is used only for plan negotiation, complex
material questions and the scheduled jobs listed in CALL_SPECS. Over budget, Sonnet routes are
downgraded to Haiku.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from agent.brain.accounting import BudgetMode
from agent.brain.budgets import ModelRole

Profile = Literal["chat", "plan", "material", "grades"]

_PLAN_RE = re.compile(
    r"(план|расписани|перенес|перенос|сдвин|отмен|забь|забить|не\s+трога|пропущ|пропуст|"
    r"верни как было|отложи|больше времени|меньше времени|разгрузи|выходной|лимит|"
    r"\breschedul|\bmove\b|\bskip\b|\bpostpone)",
    re.IGNORECASE,
)
_MATERIAL_RE = re.compile(
    r"(объясни|что такое|как работает|почему|в чём разница|в чем разница|пример|докажи|"
    r"разбери|задач[аеуи]|решени|алгоритм|теорем|\bexplain\b|\bwhy\b|\bhow does\b|\bprove\b|"
    r"\bTCP\b|\bUDP\b|\bIP\b|dijkstra|big-?o|\bDP\b|eigen|matrix|матриц|вектор|"
    r"homograph|convolution|\bCNN\b|feature)",
    re.IGNORECASE,
)
_GRADES_RE = re.compile(
    r"(оценк|балл|grade|score|сколько нужно|дедлайн|deadline|сдать|задани[ея]|assignment|"
    r"процент|\bGPA\b)",
    re.IGNORECASE,
)
_COMPLEX_HINT = re.compile(
    r"(подробно|детально|step by step|по шагам|сравни|докажи|\bprove\b)", re.I
)
_LONG_QUESTION_CHARS = 280


@dataclass(frozen=True, slots=True)
class Route:
    role: ModelRole
    profile: Profile
    reason: str


def route(text: str, *, pending_proposal: bool, budget: BudgetMode) -> Route:
    r = _route(text, pending_proposal=pending_proposal)
    if r.role == "smart" and budget in ("haiku_only", "stopped"):
        return Route("cheap", r.profile, r.reason + "+budget_downgrade")
    return r


def _route(text: str, *, pending_proposal: bool) -> Route:
    if _PLAN_RE.search(text) or (pending_proposal and _looks_like_reply(text)):
        return Route("smart", "plan", "plan_intent")
    if _MATERIAL_RE.search(text):
        complex_q = len(text) > _LONG_QUESTION_CHARS or bool(_COMPLEX_HINT.search(text))
        return Route(
            "smart" if complex_q else "cheap",
            "material",
            "material_complex" if complex_q else "material_simple",
        )
    if _GRADES_RE.search(text):
        return Route("cheap", "grades", "grades_intent")
    return Route("cheap", "chat", "default")


def _looks_like_reply(text: str) -> bool:
    """Short answers while a plan proposal is open ('а если вариант Б?') stay on the plan route."""
    return len(text) < 120 and bool(re.search(r"(вариант|а если|лучше|давай|ок|нет)", text, re.I))
