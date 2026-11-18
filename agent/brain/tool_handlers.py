"""Tool implementations. Each returns compact Russian/English text with only the fields the model
needs (never raw DB/Canvas objects), truncated to TOOL_RESULT_BUDGET by the dispatcher."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.brain.accounting import budget_state
from agent.config import Settings
from agent.core.clock import Clock
from agent.core.timeutil import local_to_utc, parse_hhmm
from agent.db.models import (
    Agreement,
    Assignment,
    Block,
    BusySlot,
    Course,
    Exam,
    Proposal,
    Reminder,
    Topic,
)
from agent.grades.service import need_for_exam, render_need
from agent.planner.blocks import debt, record_study
from agent.planner.model import DEFAULT_CUTOFFS, PlannedBlock
from agent.planner.priority import priority
from agent.planner.render import KIND_RU, fmt_day, short_exam
from agent.planner.service import (
    PlanError,
    PlanInputs,
    load_inputs,
    propose_plan,
    propose_undo,
)
from agent.prep.settings_service import SPECS, SettingsError, coerce


class ToolError(ValueError):
    """Expected, user-facing problem (bad input); returned to the model as is_error."""


@dataclass
class ToolContext:
    sessionmaker: async_sessionmaker
    settings: Settings
    clock: Clock
    proposals: list[int] = field(default_factory=list)
    escalate_reason: str | None = None
    voice_text: str | None = None


# --- lookups ---------------------------------------------------------------------------------


async def find_exam(s: AsyncSession, name: str) -> Exam:
    exams = list((await s.scalars(select(Exam))).all())
    keys = {e.title.lower(): e for e in exams} | {short_exam(e.title).lower(): e for e in exams}
    match = difflib.get_close_matches(name.lower(), list(keys), n=1, cutoff=0.4)
    if not match:
        sub = [e for k, e in keys.items() if name.lower() in k]
        if sub:
            return sub[0]
        raise ToolError(
            f"Экзамен «{name}» не найден. Есть: " + ", ".join(short_exam(e.title) for e in exams)
        )
    return keys[match[0]]


LIVE_STATUSES = ("active", "draft", "rating")  # drafts are editable during topic review


async def find_topic(s: AsyncSession, name: str, exam: str | None = None) -> Topic:
    """Fuzzy topic lookup. With an exam, a bare number ("3") refers to the position in that exam's
    review list (topics ordered by id, as shown on the review card)."""
    q = select(Topic).where(Topic.status.in_(LIVE_STATUSES)).order_by(Topic.id)
    if exam:
        q = q.where(Topic.exam_id == (await find_exam(s, exam)).id)
    topics = list((await s.scalars(q)).all())
    if exam and name.strip().isdigit():
        index = int(name.strip()) - 1
        if 0 <= index < len(topics):
            return topics[index]
        raise ToolError(f"В списке {len(topics)} тем, номера {name} нет.")
    by_name = {t.name.lower(): t for t in topics}
    match = difflib.get_close_matches(name.lower(), list(by_name), n=1, cutoff=0.45)
    if match:
        return by_name[match[0]]
    sub = [t for k, t in by_name.items() if name.lower() in k]
    if sub:
        return sub[0]
    raise ToolError(f"Тема «{name}» не найдена. Близкие: " + ", ".join(sorted(by_name)[:12]))


def _parse_local_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"Дата должна быть YYYY-MM-DD, получено {value!r}") from exc


# --- plan ------------------------------------------------------------------------------------


async def get_plan(ctx: ToolContext, args: dict[str, Any]) -> str:
    start = _parse_local_date(args["start_date"]) if args.get("start_date") else ctx.clock.today()
    days = max(1, min(7, int(args.get("days", 1))))
    begin = local_to_utc(start, parse_hhmm("00:00"), ctx.settings.zone)
    async with ctx.sessionmaker() as s:
        inputs = await load_inputs(s, ctx.settings, ctx.clock)
        rows = (
            await s.scalars(
                select(Block)
                .where(
                    Block.starts_at >= begin,
                    Block.starts_at < begin + timedelta(days=days),
                    Block.status != "moved",
                )
                .order_by(Block.starts_at)
            )
        ).all()
    planned = [
        (PlannedBlock(b.starts_at, b.ends_at, b.exam_id, b.topic_id, b.kind), b.status)
        for b in rows
    ]  # type: ignore[arg-type]
    lines = []
    for i in range(days):
        d = start + timedelta(days=i)
        items = [
            (pb, st) for pb, st in planned if pb.start.astimezone(ctx.settings.zone).date() == d
        ]
        lines.append(
            f"{fmt_day(d)}: "
            + (
                "; ".join(f"{_block_line(pb, inputs, ctx)} [{st}]" for pb, st in items)
                or "нет блоков"
            )
        )
    return "\n".join(lines)


def _block_line(b: PlannedBlock, inputs: PlanInputs, ctx: ToolContext) -> str:
    topic = inputs.topic_names.get(b.topic_id, "mock") if b.topic_id else "mock"
    exam = short_exam(inputs.exam_names.get(b.exam_id, "?"))
    return f"{b.start.astimezone(ctx.settings.zone):%H:%M} {exam} {topic} ({KIND_RU[b.kind]})"


async def get_progress(ctx: ToolContext, args: dict[str, Any]) -> str:
    async with ctx.sessionmaker() as s:
        exams = (await s.scalars(select(Exam).order_by(Exam.starts_at))).all()
        topics = (await s.scalars(select(Topic).where(Topic.status == "active"))).all()
        d = await debt(s, ctx.clock)
    lines = []
    for e in exams:
        ts = [t for t in topics if t.exam_id == e.id]
        if not ts:
            lines.append(f"{short_exam(e.title)}: тем нет")
            continue
        avg = sum(t.level for t in ts) / len(ts)
        weak = ", ".join(t.name for t in sorted(ts, key=lambda t: (t.level, -t.importance))[:3])
        lines.append(
            f"{short_exam(e.title)}: {len(ts)} тем, ср. уровень {avg:.1f}/3; слабые: {weak}"
        )
    lines.append(f"Долг: {d.blocks} блоков ({d.hours} ч)")
    return "\n".join(lines)


async def explain_priority(ctx: ToolContext, args: dict[str, Any]) -> str:
    async with ctx.sessionmaker() as s:
        inputs = await load_inputs(s, ctx.settings, ctx.clock)
        today = ctx.clock.today()
        exams = {e.id: e for e in inputs.exams}
        if args.get("topic"):
            t = await find_topic(s, args["topic"])
            info = next(x for x in inputs.topics if x.id == t.id)
            p = priority(info, exams[info.exam_id], today, inputs.cfg)
            terms = ", ".join(f"{k}={v:.2f}×{inputs.cfg.weights[k]}" for k, v in p.terms.items())
            return f"{t.name}: priority {p.total:.3f} ({terms})"
    ranked = sorted(
        (
            (priority(t, exams[t.exam_id], today, inputs.cfg).total, t)
            for t in inputs.topics
            if t.exam_id in exams
        ),
        key=lambda x: -x[0],
    )[:8]
    return "Top priorities today:\n" + "\n".join(
        f"{p:.3f} {short_exam(exams[t.exam_id].title)}: {t.name} (lvl {t.level}/{t.target_level})"
        for p, t in ranked
    )


async def propose_plan_change(ctx: ToolContext, args: dict[str, Any]) -> str:
    reason = str(args.get("reason", "")).strip()[:200] or "Изменение плана"
    async with ctx.sessionmaker() as s:
        changes = await _changes_from(s, args)
        alt = await _changes_from(s, args["alternative"]) if args.get("alternative") else None
        inputs = await load_inputs(s, ctx.settings, ctx.clock)
        try:
            p = await propose_plan(
                s,
                inputs,
                ctx.clock,
                reason=reason,
                proposed_by="agent",
                changes=changes or None,
                alt_changes=alt,
            )
        except PlanError as exc:
            raise ToolError(f"Планировщик отклонил вариант: {exc}") from exc
        await s.commit()
    if p is None:
        return "План не меняется — пересчёт дал тот же результат."
    ctx.proposals.append(p.id)
    parts = [f"Proposal #{p.id} created; the student will see buttons."]
    for o in p.options_json:
        if o["key"] != "KEEP":
            parts.append(f"Option {o['key']} diff:\n{o['diff']}{o.get('note', '')}")
    return "\n".join(parts)


async def _changes_from(s: AsyncSession, args: dict[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if args.get("pause_exam") and args.get("pause_until"):
        exam = await find_exam(s, args["pause_exam"])
        until = _parse_local_date(args["pause_until"])
        changes["paused_until"] = {exam.id: until.isoformat()}
    if args.get("day_caps"):
        caps = {}
        for item in args["day_caps"][:14]:
            hours = float(item["hours"])
            if not 0 <= hours <= 12:
                raise ToolError("Лимит на день должен быть от 0 до 12 ч")
            caps[_parse_local_date(item["date"]).isoformat()] = hours
        changes["day_caps"] = caps
    return changes


async def propose_undo_tool(ctx: ToolContext, args: dict[str, Any]) -> str:
    async with ctx.sessionmaker() as s:
        inputs = await load_inputs(s, ctx.settings, ctx.clock)
        p = await propose_undo(s, inputs, ctx.clock)
        await s.commit()
    if p is None:
        return "Нечего отменять: предыдущей версии плана нет."
    ctx.proposals.append(p.id)
    return f"Proposal #{p.id} (undo) created. Diff:\n{p.options_json[0]['diff']}"


async def propose_settings_change(ctx: ToolContext, args: dict[str, Any]) -> str:
    key = str(args["key"])
    spec = SPECS.get(key)
    if spec is None or spec.locked:
        raise ToolError(f"Настройку {key} нельзя менять через чат (только /settings).")
    try:
        value = coerce(key, args["value"])
    except SettingsError as exc:
        raise ToolError(str(exc)) from exc
    async with ctx.sessionmaker() as s:
        now = ctx.clock.now()
        p = Proposal(
            created_at=now,
            kind="settings",
            summary_ru=f"{spec.label_ru}: {value}",
            options_json=[{"key": "A", "setting": key, "value": value}, {"key": "KEEP"}],
            status="pending",
            expires_at=now + timedelta(hours=36),
        )
        s.add(p)
        await s.commit()
    ctx.proposals.append(p.id)
    return f"Proposal #{p.id}: {spec.label_ru} → {value}. Applies after the student confirms; plan is then recomputed."


# --- prep ------------------------------------------------------------------------------------


async def log_study_session(ctx: ToolContext, args: dict[str, Any]) -> str:
    minutes = int(args["minutes"])
    if not 5 <= minutes <= 600:
        raise ToolError("Минуты: от 5 до 600")
    kind = args.get("kind", "learn")
    if kind not in KIND_RU:
        raise ToolError("kind: learn|practice|review|mock")
    async with ctx.sessionmaker() as s:
        topic = await find_topic(s, args["topic"], args.get("exam"))
        await record_study(
            s, topic, minutes, kind, clock=ctx.clock, source="manual", note=args.get("note")
        )
        await s.commit()
    return f"Записал: {topic.name}, {minutes} мин ({KIND_RU[kind]})."


async def set_topic_level(ctx: ToolContext, args: dict[str, Any]) -> str:
    level = int(args["level"])
    if not 0 <= level <= 3:
        raise ToolError("Уровень 0–3")
    async with ctx.sessionmaker() as s:
        topic = await find_topic(s, args["topic"], args.get("exam"))
        old = topic.level
        topic.level, topic.progress_min = level, 0
        await s.commit()
    return f"{topic.name}: уровень {old} → {level}. План учтёт это при ночном пересчёте."


async def manage_topics(ctx: ToolContext, args: dict[str, Any]) -> str:
    action = args["action"]
    async with ctx.sessionmaker() as s:
        if action == "add":
            exam = await find_exam(s, args["exam"])
            t = Topic(
                exam_id=exam.id,
                name=str(args["name"])[:120],
                effort_hours=float(args.get("effort_hours", 3)),
                importance=int(args.get("importance", 2)),
                level=0,
                target_level=2,
                status="active",
            )
            s.add(t)
            msg = f"Добавил тему «{t.name}» в {short_exam(exam.title)}."
        elif action == "remove":
            t = await find_topic(s, args["name"], args.get("exam"))
            t.status = "dropped"
            msg = f"Убрал тему «{t.name}»."
        elif action == "rename":
            t = await find_topic(s, args["name"], args.get("exam"))
            old, t.name = t.name, str(args["new_name"])[:120]
            msg = f"«{old}» → «{t.name}»."
        elif action == "merge":
            src = await find_topic(s, args["name"], args.get("exam"))
            dst = await find_topic(s, args["into"], args.get("exam"))
            if src.id == dst.id:
                raise ToolError("Нельзя объединить тему саму с собой")
            src.status, src.merged_into_id = "merged", dst.id
            dst.effort_hours += src.effort_hours * 0.5
            msg = f"«{src.name}» объединена с «{dst.name}»."
        else:
            raise ToolError("action: add|remove|rename|merge")
        await s.commit()
    return msg + " План обновится при следующем пересчёте."


# --- grades ----------------------------------------------------------------------------------


async def get_course_scores(ctx: ToolContext, args: dict[str, Any]) -> str:
    async with ctx.sessionmaker() as s:
        rows = (
            await s.execute(
                select(Exam, Course)
                .outerjoin(Course, Exam.course_id == Course.id)
                .order_by(Exam.starts_at)
            )
        ).all()
    return "\n".join(
        f"{short_exam(e.title)}: "
        + (
            f"{c.current_score:.1f}% ({c.current_grade})"
            if c and c.current_score is not None
            else "нет данных"
        )
        + (
            f", final {e.final_weight:.0%}"
            if e.final_weight is not None
            else ", вес final неизвестен"
        )
        for e, c in rows
    )


async def calc_needed_final(ctx: ToolContext, args: dict[str, Any]) -> str:
    letters = [args["target"]] if args.get("target") else ["A+", "A", "A-", "B+"]
    if any(letter not in DEFAULT_CUTOFFS for letter in letters):
        raise ToolError(f"Неизвестная оценка; есть: {', '.join(DEFAULT_CUTOFFS)}")
    async with ctx.sessionmaker() as s:
        exam = await find_exam(s, args["course"])
        result = await need_for_exam(s, exam)
    return re.sub(r"</?(b|i)>", "", render_need(exam, result, letters))


async def set_exam_info(ctx: ToolContext, args: dict[str, Any]) -> str:
    """The student states a final weight or a target grade ("вес final по алгебре 60%")."""
    async with ctx.sessionmaker() as s:
        exam = await find_exam(s, args["exam"])
        parts = []
        if args.get("final_weight") is not None:
            w = float(args["final_weight"])
            w = w / 100 if w > 1 else w
            if not 0 < w <= 1:
                raise ToolError("Вес final: от 1 до 100%")
            exam.final_weight, exam.weight_source = w, "user"
            parts.append(f"вес final {w:.0%}")
        if args.get("target_grade"):
            if args["target_grade"] not in DEFAULT_CUTOFFS:
                raise ToolError(f"Цель: одна из {', '.join(DEFAULT_CUTOFFS)}")
            exam.target_grade = args["target_grade"]
            parts.append(f"цель {exam.target_grade}")
        if not parts:
            raise ToolError("Укажи final_weight и/или target_grade")
        await s.commit()
    return (
        f"{short_exam(exam.title)}: " + ", ".join(parts) + ". Приоритеты учтут это при пересчёте."
    )


async def add_busy_time(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Block time the planner must not use (lectures, appointments), then propose a replan."""
    try:
        start, end = parse_hhmm(args["start"]), parse_hhmm(args["end"])
    except ValueError as exc:
        raise ToolError("Время в формате HH:MM") from exc
    if end <= start:
        raise ToolError("Конец должен быть позже начала")
    on_date = _parse_local_date(args["date"]) if args.get("date") else None
    weekday = int(args["weekday"]) if args.get("weekday") is not None else None
    if (on_date is None) == (weekday is None):
        raise ToolError("Укажи либо date (разово), либо weekday 0–6 (каждую неделю)")
    if weekday is not None and not 0 <= weekday <= 6:
        raise ToolError("weekday: 0=пн … 6=вс")
    until = _parse_local_date(args["until"]) if args.get("until") else None
    async with ctx.sessionmaker() as s:
        s.add(
            BusySlot(
                weekday=weekday,
                on_date=on_date,
                start_local=args["start"],
                end_local=args["end"],
                label=str(args.get("label") or "занято")[:120],
                valid_from=ctx.clock.today(),
                valid_until=until,
            )
        )
        await s.commit()
        inputs = await load_inputs(s, ctx.settings, ctx.clock)
        try:
            p = await propose_plan(
                s,
                inputs,
                ctx.clock,
                reason=f"Учёт занятого времени: {args.get('label') or ''}",
                proposed_by="agent",
            )
        except PlanError as exc:
            raise ToolError(f"Время добавлено, но план не пересчитался: {exc}") from exc
        await s.commit()
    if p is None:
        return "Время добавлено; план от этого не меняется."
    ctx.proposals.append(p.id)
    return f"Время добавлено. Proposal #{p.id} учитывает его; студент подтвердит кнопкой."


async def get_deadlines(ctx: ToolContext, args: dict[str, Any]) -> str:
    async with ctx.sessionmaker() as s:
        rows = (
            await s.scalars(
                select(Assignment)
                .where(Assignment.due_at >= ctx.clock.now(), Assignment.user_state.is_(None))
                .order_by(Assignment.due_at)
                .limit(15)
            )
        ).all()
    if not rows:
        return "Ближайших дедлайнов в базе нет (задания синхронизируются с Canvas)."
    zone = ctx.settings.zone
    return "\n".join(
        f"{fmt_day(a.due_at.astimezone(zone).date())} {a.due_at.astimezone(zone):%H:%M} — "
        f"{a.name[:60]} [{a.submission_state or '?'}]"
        for a in rows
    )


# --- memory & reminders ----------------------------------------------------------------------


async def save_agreement(ctx: ToolContext, args: dict[str, Any]) -> str:
    async with ctx.sessionmaker() as s:
        exam_id = (await find_exam(s, args["exam"])).id if args.get("exam") else None
        a = Agreement(
            text=str(args["text"])[:300], exam_id=exam_id, active=True, created_at=ctx.clock.now()
        )
        s.add(a)
        await s.commit()
    return f"Договорённость #{a.id} сохранена. (Если она меняет план — нужен propose_plan_change.)"


async def list_agreements(ctx: ToolContext, args: dict[str, Any]) -> str:
    async with ctx.sessionmaker() as s:
        rows = (
            await s.scalars(
                select(Agreement).where(Agreement.active.is_(True)).order_by(Agreement.id)
            )
        ).all()
    return "\n".join(f"#{a.id} {a.text}" for a in rows) or "Договорённостей нет."


async def add_personal_reminder(ctx: ToolContext, args: dict[str, Any]) -> str:
    try:
        local = datetime.strptime(args["at"], "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ToolError("at: 'YYYY-MM-DD HH:MM' по HKT") from exc
    when = local.replace(tzinfo=ctx.settings.zone)
    if when <= ctx.clock.now():
        raise ToolError("Это время уже прошло")
    async with ctx.sessionmaker() as s:
        s.add(
            Reminder(
                text=str(args["text"])[:300],
                remind_at=when,
                status="pending",
                source="user",
                created_at=ctx.clock.now(),
            )
        )
        await s.commit()
    return f"Напомню {fmt_day(local.date())} в {local:%H:%M}."


async def list_reminders(ctx: ToolContext, args: dict[str, Any]) -> str:
    async with ctx.sessionmaker() as s:
        rows = (
            await s.scalars(
                select(Reminder)
                .where(Reminder.status == "pending")
                .order_by(Reminder.remind_at)
                .limit(10)
            )
        ).all()
    z = ctx.settings.zone
    return (
        "\n".join(
            f"{fmt_day(r.remind_at.astimezone(z).date())} {r.remind_at.astimezone(z):%H:%M} — {r.text[:80]}"
            for r in rows
        )
        or "Напоминаний нет."
    )


async def reply_with_voice(ctx: ToolContext, args: dict[str, Any]) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("text: короткая суть ответа для озвучки")
    ctx.voice_text = text[:300]
    return "Voice gist queued; still write the full answer as text (lists/schedules stay in text)."


async def escalate(ctx: ToolContext, args: dict[str, Any]) -> str:
    ctx.escalate_reason = str(args.get("reason", "complex"))[:120]
    return "Escalating to the stronger model."


async def get_usage(ctx: ToolContext, args: dict[str, Any]) -> str:
    async with ctx.sessionmaker() as s:
        st = await budget_state(
            s, ctx.clock, ctx.settings.daily_llm_budget_usd, ctx.settings.llm_budget_warn_pct
        )
    return f"LLM сегодня: ${st.spent:.3f} из ${st.budget:.2f} ({st.pct:.0f}%), режим {st.mode}."
