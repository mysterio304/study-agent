"""Grade views backed by the synced Canvas data: /need, /grades, /deadlines (templates)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.core.clock import Clock
from agent.db.models import Assignment, AssignmentGroup, Course, Exam
from agent.grades.calculator import AssignmentIn, GroupIn, NeedResult, needed_on_final
from agent.notify.ladder import is_open
from agent.planner.model import DEFAULT_CUTOFFS
from agent.planner.render import short_exam
from agent.telegram.format import fmt_dt, h

ESTIMATE_NOTE = "Оценка приблизительная: drop rules, кривые и округление не учтены."


async def need_for_exam(session: AsyncSession, exam: Exam) -> NeedResult:
    course = await session.get(Course, exam.course_id) if exam.course_id else None
    if course is None:
        return NeedResult(exam.final_weight, "exam" if exam.final_weight else "unknown", None, None)
    groups = (
        await session.scalars(select(AssignmentGroup).where(AssignmentGroup.course_id == course.id))
    ).all()
    items = (
        await session.scalars(select(Assignment).where(Assignment.course_id == course.id))
    ).all()
    return needed_on_final(
        [GroupIn(g.id, g.name, g.group_weight) for g in groups],
        [
            AssignmentIn(
                a.group_id, a.points_possible, a.score, a.name, a.excused, a.omit_from_final_grade
            )
            for a in items
        ],
        weighted=course.apply_group_weights,
        exam_final_weight=exam.final_weight,
        canvas_current=course.current_score,
        cutoffs=DEFAULT_CUTOFFS,
    )


def _verdict(x: float) -> str:
    if x > 100:
        return "недостижимо"
    if x <= 0:
        return "уже гарантировано"
    return f"<b>{x:.1f}%</b>"


def render_need(exam: Exam, r: NeedResult, letters: Sequence[str]) -> str:
    name = h(short_exam(exam.title))
    if r.final_weight is None:
        return f"{name}: вес final неизвестен — напиши его (например «вес final по алгебре 60%»)."
    if r.coursework_avg is None:
        return f"{name}: в Canvas ещё нет оценок за курсовую."
    src = "из Canvas" if r.weight_source == "canvas" else "по syllabus/с твоих слов"
    lines = [
        f"<b>{name}</b>: final {r.final_weight:.0%} ({src}), курсовая сейчас {r.coursework_avg:.1f}%"
    ]
    for letter in letters:
        need = r.need.get(letter)
        if need is None:
            continue
        line = f"{letter} (≥{need.cutoff:g}%): на final нужно {_verdict(need.if_current)}"
        if abs(need.if_zero - need.if_current) > 0.5:
            line += f"; если несданное = 0 → {_verdict(need.if_zero)}"
        lines.append(line)
    lines.append(f"<i>{ESTIMATE_NOTE}</i>")
    return "\n".join(lines)


async def deadlines_view(session: AsyncSession, clock: Clock, days: int = 14) -> str:
    now = clock.now()
    rows = (
        await session.execute(
            select(Assignment, Course)
            .join(Course, Assignment.course_id == Course.id)
            .where(
                Assignment.due_at > now - timedelta(days=3),
                Assignment.due_at <= now + timedelta(days=days),
            )
            .order_by(Assignment.due_at)
        )
    ).all()
    lines = [f"<b>Дедлайны на {days} дн.</b>"]
    for a, c in rows:
        if a.user_state == "ignored":
            continue
        if not is_open(a):
            mark = "✅"  # submitted, excused, on paper, or closed with «Сделал»
        elif a.due_at < now:
            mark = "⚠️ missing" if a.is_missing else "просрочено"
        else:
            mark = "⏳"
        lines.append(
            f"{mark} {fmt_dt(a.due_at, clock.zone)} — {h(c.course_code or c.name)}: {h(a.name)}"
        )
    return "\n".join(lines) if len(lines) > 1 else "Ближайших дедлайнов нет 🎉"


async def grades_view(session: AsyncSession, clock: Clock) -> str:
    exams = (await session.scalars(select(Exam).order_by(Exam.starts_at))).all()
    lines = ["<b>Оценки</b>"]
    for e in exams:
        course = await session.get(Course, e.course_id) if e.course_id else None
        score = (
            f"{course.current_score:.1f}% ({h(course.current_grade)})"
            if course and course.current_score is not None
            else "нет данных"
        )
        lines.append(f"{h(short_exam(e.title))}: {score}")
    recent = (
        await session.execute(
            select(Assignment, Course)
            .join(Course, Assignment.course_id == Course.id)
            .where(Assignment.score.is_not(None))
            .order_by(Assignment.synced_at.desc())
            .limit(5)
        )
    ).all()
    if recent:
        lines.append("\nПоследние:")
        lines += [
            f"• {h(c.course_code or c.name)}: {h(a.name)} — {a.score:g}/{(a.points_possible or 0):g}"
            for a, c in recent
        ]
    lines.append("\n/need &lt;курс&gt; &lt;оценка&gt; — сколько нужно на final")
    return "\n".join(lines)
