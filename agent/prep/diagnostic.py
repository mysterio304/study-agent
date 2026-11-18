"""Optional per-course diagnostic (/diagnostic <course>): 2 quiz polls per topic, generated in one
batch (on-demand fallback); when all are answered, level changes are proposed — never applied
silently (your self-ratings stay unless you confirm)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from agent.db.models import Exam, Quiz, Setting, Topic
from agent.flows import Services, send_proposal
from agent.notify.quizpolls import send_quiz_polls
from agent.prep.quizzes import start_quiz
from agent.prep.topic_proposals import create_topic_proposal, diagnostic_changes, proposal_title

log = logging.getLogger(__name__)

PER_TOPIC = 2


def _key(exam_id: int) -> str:
    return f"diagnostic:{exam_id}"


async def start_diagnostic(svc: Services, exam_id: int) -> str:
    """Returns a Russian status line for the student."""
    if svc.quizzes is None:
        return "Диагностика недоступна: нужен ANTHROPIC_API_KEY."
    async with svc.sessionmaker() as s:
        topics = list(
            (
                await s.scalars(
                    select(Topic.id).where(Topic.exam_id == exam_id, Topic.status == "active")
                )
            ).all()
        )
    if not topics:
        return "У этого курса пока нет тем."
    state = await svc.quizzes.fill(topics, per_topic=PER_TOPIC, meta={"diagnostic_exam": exam_id})
    if state == "batched":
        return "🧪 Готовлю вопросы по всем темам (до ~20 минут) — пришлю, как будут готовы."
    return await send_diagnostic(svc, exam_id)


async def on_bank_ready(svc: Services, meta: dict[str, Any]) -> None:
    exam_id = meta.get("diagnostic_exam")
    if exam_id:
        status = await send_diagnostic(svc, int(exam_id))
        if status:
            await svc.notifier.send(status)


async def send_diagnostic(svc: Services, exam_id: int) -> str:
    async with svc.sessionmaker() as s:
        exam = await s.get(Exam, exam_id)
        topics = (
            await s.scalars(
                select(Topic)
                .where(Topic.exam_id == exam_id, Topic.status == "active")
                .order_by(Topic.id)
            )
        ).all()
        quiz_ids = []
        for t in topics:
            quiz = await start_quiz(
                s, t.id, svc.clock, purpose="diagnostic", block_id=None, n=PER_TOPIC
            )
            if quiz is not None:
                quiz_ids.append(quiz.id)
        row = await s.get(Setting, _key(exam_id)) or Setting(key=_key(exam_id))
        row.value_json, row.updated_at, row.updated_by = (
            {"quiz_ids": quiz_ids},
            svc.clock.now(),
            "system",
        )
        s.add(row)
        await s.commit()
    if not quiz_ids:
        return "Не нашёл материалов для вопросов — сначала нужны проиндексированные файлы курса."
    await svc.notifier.send(
        f"🧪 Диагностика {exam.title if exam else ''}: {len(quiz_ids)} тем × {PER_TOPIC} "
        "вопроса. Отвечай в любом порядке; в конце предложу уровни."
    )
    for qid in quiz_ids:
        await send_quiz_polls(svc, qid)
    return ""


async def maybe_finish(svc: Services, quiz_id: int) -> None:
    """After a diagnostic quiz finished: if its run is complete, propose level changes."""
    async with svc.sessionmaker() as s:
        quiz = await s.get(Quiz, quiz_id)
        if quiz is None or quiz.purpose != "diagnostic":
            return
        topic = await s.get(Topic, quiz.topic_id)
        row = await s.get(Setting, _key(topic.exam_id)) if topic else None
        ids = (row.value_json or {}).get("quiz_ids", []) if row else []
        if not ids or quiz_id not in ids:
            return
        done = (
            await s.scalars(select(Quiz.id).where(Quiz.id.in_(ids), Quiz.finished_at.is_not(None)))
        ).all()
        if len(done) < len(ids):
            return
        exam = await s.get(Exam, topic.exam_id)
        changes = await diagnostic_changes(s, ids)
        p = await create_topic_proposal(
            s, "topic_levels", proposal_title("topic_levels", exam), changes, svc.clock
        )
        row.value_json = {"quiz_ids": [], "finished": True}
        await s.commit()
    if p is None:
        await svc.notifier.send(
            "Диагностика завершена: твои оценки уровней совпали с результатами 👍"
        )
    else:
        await send_proposal(svc, p.id)
