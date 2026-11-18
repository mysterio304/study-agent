"""Message templates (no LLM): proposal cards, plan views, pings, digests, /usage."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.brain.accounting import BudgetState, local_day_start
from agent.config import Settings
from agent.core.clock import Clock
from agent.core.timeutil import local_to_utc
from agent.db.models import Block, Exam, LlmUsage, Proposal, Topic
from agent.planner.blocks import debt
from agent.planner.model import PlannedBlock
from agent.planner.render import KIND_RU, render_day, short_exam
from agent.telegram.format import h
from agent.telegram.keyboards import proposal_kb


def proposal_card(p: Proposal) -> tuple[str, InlineKeyboardMarkup]:
    if p.kind == "settings":
        text = f"<b>Изменить настройку?</b>\n{h(p.summary_ru)}\nПосле применения пересчитаю план."
        return text, proposal_kb(p.id, p.options_json, single_label="✅ Применить")
    lines = [f"<b>Предложение #{p.id}</b>: {h(p.summary_ru)}"]
    for o in p.options_json:
        if o["key"] == "KEEP":
            continue
        lines.append(
            f"\n<b>Вариант {o['key']}</b> — блоков по дням:\n{h(o['diff'])}{h(o.get('note', ''))}"
        )
    return "\n".join(lines), proposal_kb(p.id, p.options_json, single_label="✅ Принять")


async def _names(session: AsyncSession) -> tuple[dict[int, str], dict[int, str]]:
    exams = {e.id: e.title for e in (await session.scalars(select(Exam))).all()}
    topics = {t.id: t.name for t in (await session.scalars(select(Topic))).all()}
    return exams, topics


async def day_view(session: AsyncSession, settings: Settings, day: date) -> str:
    zone = settings.zone
    start = local_day_start_for(day, settings)
    rows = (
        await session.scalars(
            select(Block)
            .where(
                Block.starts_at >= start,
                Block.starts_at < start + timedelta(days=1),
                Block.status != "moved",
            )
            .order_by(Block.starts_at)
        )
    ).all()
    exams, topics = await _names(session)
    planned = [PlannedBlock(b.starts_at, b.ends_at, b.exam_id, b.topic_id, b.kind) for b in rows]  # type: ignore[arg-type]
    statuses = {pb: b.status for pb, b in zip(planned, rows, strict=True)}
    return render_day(day, planned, zone, exams, topics, statuses)


def local_day_start_for(day: date, settings: Settings) -> datetime:
    return local_to_utc(day, time(0, 0), settings.zone)


async def exams_line(session: AsyncSession, settings: Settings, today: date) -> str:
    parts = []
    for e in (await session.scalars(select(Exam).order_by(Exam.starts_at))).all():
        days = (e.starts_at.astimezone(settings.zone).date() - today).days
        if days >= 0:
            parts.append(
                f"{h(short_exam(e.title))} {'сегодня' if days == 0 else f'через {days} дн.'}"
            )
    return " · ".join(parts)


async def morning_digest(session: AsyncSession, settings: Settings, clock: Clock) -> str:
    today = clock.today()
    d = await debt(session, clock)
    lines = [
        f"☀️ Доброе утро! {await exams_line(session, settings, today)}",
        "",
        await day_view(session, settings, today),
    ]
    if d.blocks:
        lines.append(f"\nДолг: {d.blocks} блоков ({d.hours} ч) — ночью перераспределю.")
    return "\n".join(lines)


async def progress_view(session: AsyncSession) -> str:
    exams = (await session.scalars(select(Exam).order_by(Exam.starts_at))).all()
    lines = ["<b>Прогресс</b>"]
    for e in exams:
        topics = (
            await session.scalars(
                select(Topic)
                .where(Topic.exam_id == e.id, Topic.status == "active")
                .order_by(Topic.level, Topic.id)
            )
        ).all()
        if not topics:
            lines.append(f"\n<b>{h(short_exam(e.title))}</b>: тем пока нет")
            continue
        avg = sum(t.level for t in topics) / len(topics)
        bar = "▰" * round(avg * 10 / 3) + "▱" * (10 - round(avg * 10 / 3))
        lines.append(f"\n<b>{h(short_exam(e.title))}</b> {bar} {avg:.1f}/3")
        lines += [f"  {t.level}/{t.target_level} {h(t.name)}" for t in topics[:12]]
    return "\n".join(lines)


async def usage_view(session: AsyncSession, clock: Clock, state: BudgetState) -> str:
    since_day = local_day_start(clock)
    since_week = since_day - timedelta(days=6)
    rows = (
        await session.execute(
            select(
                LlmUsage.purpose,
                LlmUsage.model,
                func.count(),
                func.sum(LlmUsage.cost_usd),
                func.sum(LlmUsage.input_tokens),
                func.sum(LlmUsage.cache_read_tokens),
                func.sum(LlmUsage.cache_write_5m_tokens + LlmUsage.cache_write_1h_tokens),
            )
            .where(LlmUsage.at >= since_week)
            .group_by(LlmUsage.purpose, LlmUsage.model)
        )
    ).all()
    week = sum(r[3] or 0 for r in rows)
    fresh = sum(r[4] or 0 for r in rows)
    read = sum(r[5] or 0 for r in rows)
    write = sum(r[6] or 0 for r in rows)
    hit = 100 * read / (fresh + read + write) if fresh + read + write else 0
    mode_ru = {
        "normal": "норма",
        "warn": "⚠️ близко к лимиту",
        "haiku_only": "только Haiku",
        "stopped": "LLM выключен до полуночи",
    }
    lines = [
        "<b>LLM usage</b>",
        f"Сегодня: ${state.spent:.3f} из ${state.budget:.2f} ({state.pct:.0f}%) — {mode_ru[state.mode]}",
        f"7 дней: ${week:.3f} · cache hit {hit:.0f}%",
    ]
    by_purpose: dict[str, float] = defaultdict(float)
    by_model: dict[str, float] = defaultdict(float)
    for purpose, model, _n, cost, *_ in rows:
        by_purpose[purpose] += cost or 0
        by_model[model] += cost or 0
    if by_purpose:
        lines.append(
            "По задачам: "
            + ", ".join(f"{k} ${v:.3f}" for k, v in sorted(by_purpose.items(), key=lambda x: -x[1]))
        )
        lines.append(
            "По моделям: "
            + ", ".join(f"{k.removeprefix('claude-')} ${v:.3f}" for k, v in by_model.items())
        )
    return "\n".join(lines)


def block_ping(b: Block, exam: str, topic: str, zone: ZoneInfo, nth: int) -> str:
    start = b.starts_at.astimezone(zone)
    what = f"{h(short_exam(exam))}: {h(topic)} <i>({KIND_RU[b.kind]})</i>"
    if nth == 0:
        return f"⏰ {start:%H:%M} — {what}. Начинаем?"
    if nth == 1:
        return f"Напоминаю: блок {what} уже идёт 15 минут."
    return f"Похоже, блок {what} не состоялся. Перенести его?"


async def morning_brief_text(session: AsyncSession, settings: Settings, clock: Clock) -> str:
    """Speakable 1–3 sentence brief for the optional morning voice message (template, no LLM)."""
    zone, today = settings.zone, clock.today()
    start = local_day_start_for(today, settings)
    blocks = (
        await session.scalars(
            select(Block)
            .where(
                Block.starts_at >= start,
                Block.starts_at < start + timedelta(days=1),
                Block.status == "planned",
            )
            .order_by(Block.starts_at)
        )
    ).all()
    parts = ["Доброе утро!"]
    if blocks:
        first = blocks[0]
        exam = await session.get(Exam, first.exam_id)
        topic = await session.get(Topic, first.topic_id) if first.topic_id else None
        what = f"{short_exam(exam.title) if exam else ''}, {topic.name if topic else 'пробный экзамен'}"
        parts.append(
            f"Сегодня {len(blocks)} блоков, первый в {first.starts_at.astimezone(zone):%H:%M}: {what}."
        )
    else:
        parts.append("Сегодня учебных блоков нет.")
    upcoming = (
        await session.scalars(
            select(Exam).where(Exam.starts_at > clock.now()).order_by(Exam.starts_at)
        )
    ).first()
    if upcoming:
        days = (upcoming.starts_at.astimezone(zone).date() - today).days
        parts.append(
            f"До экзамена {short_exam(upcoming.title)} {days} дней."
            if days
            else f"Сегодня экзамен {short_exam(upcoming.title)}. Удачи!"
        )
    return " ".join(parts)
