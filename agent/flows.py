"""Application flows shared by Telegram handlers and scheduled jobs (no aiogram handler code)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.brain.agent import Agent
from agent.brain.batches import BatchRunner
from agent.brain.llm import LlmClient
from agent.canvas.api import CanvasSource
from agent.config import Settings
from agent.core.clock import Clock
from agent.db.models import Exam, Proposal, Topic
from agent.planner.blocks import has_plan
from agent.planner.render import short_exam
from agent.planner.service import PlanError, apply_proposal, load_inputs, propose_plan
from agent.prep import topics_service as ts
from agent.prep.discovery import TopicDiscovery
from agent.prep.settings_service import SettingsError, set_value
from agent.telegram.cards import proposal_card
from agent.telegram.format import h
from agent.telegram.keyboards import rating_kb, topic_review_kb
from agent.telegram.notifier import Notifier

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    sessionmaker: async_sessionmaker
    clock: Clock
    canvas: CanvasSource
    notifier: Notifier
    llm: LlmClient | None = None
    agent: Agent | None = None
    batches: BatchRunner | None = None
    discovery: TopicDiscovery | None = None


async def send_proposal(svc: Services, proposal_id: int) -> None:
    async with svc.sessionmaker() as s:
        p = await s.get(Proposal, proposal_id)
        if p is None or p.status != "pending":
            return
        text, kb = proposal_card(p)
    msg_id = await svc.notifier.send(text, kb)
    if msg_id:
        async with svc.sessionmaker() as s:
            (await s.get(Proposal, proposal_id)).tg_message_id = msg_id
            await s.commit()


async def propose_replan(svc: Services, reason: str, *, send: bool = True) -> int | None:
    async with svc.sessionmaker() as s:
        inputs = await load_inputs(s, svc.settings, svc.clock)
        try:
            p = await propose_plan(s, inputs, svc.clock, reason=reason, proposed_by="planner")
        except PlanError:
            log.exception("Planner produced an invalid plan; not proposing")
            return None
        await s.commit()
    if p and send:
        await send_proposal(svc, p.id)
    return p.id if p else None


async def apply_choice(svc: Services, proposal_id: int, option: str) -> str:
    """Handle a proposal button. Returns a short Russian status line."""
    async with svc.sessionmaker() as s:
        p = await s.get(Proposal, proposal_id)
        if p is None:
            return "Предложение не найдено."
        kind = p.kind
        try:
            if kind == "settings":
                if p.status != "pending":
                    return "Это предложение уже неактуально."
                p.chosen_option = option
                if option == "KEEP":
                    p.status = "rejected"
                    await s.commit()
                    return "Оставил как было."
                opt = p.options_json[0]
                await set_value(s, opt["setting"], opt["value"], svc.clock, by="proposal")
                p.status = "applied"
                await s.commit()
            else:
                version = await apply_proposal(s, proposal_id, option, svc.clock)
                await s.commit()
                return (
                    "Оставил план как есть."
                    if version is None
                    else f"План обновлён (версия #{version.id})."
                )
        except (PlanError, SettingsError) as exc:
            await s.commit()
            return str(exc)
    await propose_replan(svc, "Пересчёт после изменения настроек")
    return "Настройка применена."


# --- setup wizard steps ----------------------------------------------------------------------


async def on_topics_ready(svc: Services, exam_id: int) -> None:
    async with svc.sessionmaker() as s:
        exam = await s.get(Exam, exam_id)
        drafts = await ts.topics_with_status(s, exam_id, "draft")
    if not drafts:
        await svc.notifier.send(f"Для «{h(exam.title)}» не нашёл тем в Canvas — добавь их в чате.")
        return
    lines = [f"<b>Темы: {h(short_exam(exam.title))}</b>"]
    lines += [
        f"{i}. {h(t.name)} — {'★' * t.importance}, ~{t.effort_hours:g} ч"
        for i, t in enumerate(drafts, 1)
    ]
    if exam.final_weight is not None and exam.weight_source == "syllabus":
        lines.append(f"\nВ syllabus: final = {exam.final_weight:.0%}. Если неверно — напиши.")
    lines.append("\nМожно написать: «убери 3», «объедини 4 и 5», «добавь тему Sockets».")
    await svc.notifier.send("\n".join(lines), topic_review_kb(exam_id))


async def continue_setup(svc: Services) -> None:
    """Advance the wizard: next self-rating question, or the first plan when everything is rated."""
    async with svc.sessionmaker() as s:
        topic = await ts.next_to_rate(s)
        if topic is not None:
            exam = await s.get(Exam, topic.exam_id)
            left = len(await ts.topics_with_status(s, topic.exam_id, "rating"))
        complete = await ts.setup_complete(s)
        planned = await has_plan(s)
    if topic is not None:
        await svc.notifier.send(
            f"Оцени уровень — <b>{h(short_exam(exam.title))}</b> (осталось {left}):\n<b>{h(topic.name)}</b>",
            rating_kb(topic.id),
        )
        return
    if complete and not planned:
        await svc.notifier.send(
            "Все темы оценены. Считаю совместный план для всех четырёх экзаменов…"
        )
        if await propose_replan(svc, "Первый план подготовки") is None:
            await svc.notifier.send("Не удалось построить план — проверь темы (/progress).")


async def pending_drafts(svc: Services) -> list[int]:
    async with svc.sessionmaker() as s:
        rows = (
            await s.scalars(select(Topic.exam_id).where(Topic.status == "draft").distinct())
        ).all()
    return list(rows)
