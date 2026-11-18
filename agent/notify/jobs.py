"""Scheduled jobs (ARCHITECTURE §4). All messages are templates; none of these call the LLM
except through the batch poller (results of earlier submissions).

- tick (every minute): missed blocks → debt (+ one auto-move in small_auto), block start pings
  with gentle escalation, block end check, personal reminders, batch polling, proposal expiry.
- morning digest, evening check-in, nightly replan.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Block, Exam, Proposal, Reminder, Topic
from agent.flows import Services, propose_replan, quiet_range, send_proposal
from agent.notify.ladder import run_ladder
from agent.notify.outbox import flush, mark_digest_sent, pending_digest
from agent.notify.quizpolls import send_quiz_polls
from agent.notify.recheck import refresh_message
from agent.notify.report import weekly_report
from agent.planner.blocks import auto_move_missed, has_plan, mark_missed_blocks
from agent.planner.render import fmt_day
from agent.planner.service import load_inputs
from agent.prep import checkin
from agent.prep.diagnostic import maybe_finish
from agent.prep.mock import due_pings as mock_due_pings
from agent.prep.mock import expire_unanswered as mock_expire_unanswered
from agent.prep.quizzes import (
    QUESTIONS_PER_BLOCK,
    expire_stale,
    generate_on_demand,
    start_quiz,
    unused_count,
)
from agent.prep.settings_service import effective
from agent.telegram.cards import block_ping, day_view, morning_brief_text, morning_digest
from agent.telegram.format import h
from agent.telegram.keyboards import block_end_kb, block_start_kb, checkin_kb
from agent.telegram.mock import SCORE_QUESTION

log = logging.getLogger(__name__)

REPING_AFTER = timedelta(minutes=15)
END_ASKED = 10  # ping_count marker: the end-of-block question was sent
AUTO_MOVES_PER_DAY = 1


async def tick(svc: Services) -> None:
    steps = (
        _missed,
        _block_pings,
        _block_ends,
        _reminders,
        _expire_proposals,
        _expire_quizzes,
        _mock_pings,
        _ladder,
        _flush_outbox,
    )
    for step in steps:
        try:
            await step(svc)
        except Exception:  # one failing step must not block the others
            log.exception("tick step %s failed", step.__name__)
    if svc.batches is not None:
        await svc.batches.poll()


async def _missed(svc: Services) -> None:
    async with svc.sessionmaker() as s:
        missed = await mark_missed_blocks(s, svc.clock)
        await s.commit()
    if not missed or svc.settings.autonomy_mode != "small_auto":
        return
    async with svc.sessionmaker() as s:
        start = svc.clock.now() - timedelta(days=1)
        auto_today = (
            await s.scalars(select(Block).where(Block.status == "moved", Block.starts_at >= start))
        ).all()
        if len(auto_today) >= AUTO_MOVES_PER_DAY:
            return
        inputs = await load_inputs(s, svc.settings, svc.clock)
        moved = await auto_move_missed(s, missed[0].id, inputs, svc.clock)
        await s.commit()
    if moved:
        when = moved.starts_at.astimezone(svc.settings.zone)
        await svc.notifier.send(
            f"↪️ Пропущенный блок перенёс на {fmt_day(when.date())} {when:%H:%M} (режим small_auto). "
            "Остальные пропуски — в долг, ночью перераспределю. /undo — вернуть как было."
        )


async def _names(s: AsyncSession, b: Block) -> tuple[str, str]:
    exam = await s.get(Exam, b.exam_id)
    topic = await s.get(Topic, b.topic_id) if b.topic_id else None
    return (exam.title if exam else "?"), (topic.name if topic else "полный вариант экзамена")


async def _block_pings(svc: Services) -> None:
    now = svc.clock.now()
    async with svc.sessionmaker() as s:
        due = (
            await s.scalars(
                select(Block).where(
                    Block.status == "planned",
                    Block.starts_at <= now,
                    Block.ends_at > now,
                    Block.ping_count < 3,
                )
            )
        ).all()
        to_send = []
        for b in due:
            last = b.last_ping_at or b.starts_at - REPING_AFTER
            if b.ping_count == 0 or now - last >= REPING_AFTER:
                exam, topic = await _names(s, b)
                to_send.append((b.id, block_ping(b, exam, topic, svc.settings.zone, b.ping_count)))
                b.ping_count += 1
                b.last_ping_at = now
        await s.commit()
    for block_id, text in to_send:
        await svc.notifier.send(text, block_start_kb(block_id))


async def _block_ends(svc: Services) -> None:
    now = svc.clock.now()
    async with svc.sessionmaker() as s:
        ended = (
            await s.scalars(
                select(Block).where(
                    Block.status == "started", Block.ends_at <= now, Block.ping_count < END_ASKED
                )
            )
        ).all()
        to_send = []
        for b in ended:
            _exam, topic = await _names(s, b)
            to_send.append((b.id, b.topic_id, topic))
            b.ping_count = END_ASKED  # committed before sending: never asked twice
        await s.commit()
    for block_id, topic_id, topic in to_send:
        if topic_id is not None and await send_verification_quiz(svc, block_id, topic_id, topic):
            continue
        await svc.notifier.send(f"Блок закончился: {h(topic)}. Как прошло?", block_end_kb(block_id))


async def send_verification_quiz(svc: Services, block_id: int, topic_id: int, topic: str) -> bool:
    """2–3 quiz polls from the course files; False → caller falls back to ✅/✖️ buttons."""
    async with svc.sessionmaker() as s:
        short = await unused_count(s, topic_id) < QUESTIONS_PER_BLOCK
    if short and svc.llm is not None:  # nightly batch didn't cover it → one on-demand call
        await generate_on_demand(svc.sessionmaker, svc.llm, topic_id, svc.clock)
    async with svc.sessionmaker() as s:
        quiz = await start_quiz(
            s, topic_id, svc.clock, purpose="verification", block_id=block_id, n=QUESTIONS_PER_BLOCK
        )
        await s.commit()
    if quiz is None:
        return False
    await svc.notifier.send(
        f"Блок закончился: <b>{h(topic)}</b>. {len(quiz.questions_json)} вопроса — "
        "чтобы засчитать блок (≥ 2/3 верно):"
    )
    if not await send_quiz_polls(svc, quiz.id):
        await svc.notifier.send("Не получилось отправить квиз — блок засчитан без проверки.")
    return True


MOCK_PING_TEXT = {
    "half": "⏱ Половина времени mock-экзамена прошла.",
    "last10": "⏱ Осталось 10 минут — проверь ответы.",
}


async def _mock_pings(svc: Services) -> None:
    async with svc.sessionmaker() as s:
        pings = await mock_due_pings(s, svc.clock)
        await mock_expire_unanswered(s, svc.clock)
        await s.commit()
    for p in pings:
        await svc.notifier.send(SCORE_QUESTION if p.kind == "end" else MOCK_PING_TEXT[p.kind])


async def _expire_quizzes(svc: Services) -> None:
    async with svc.sessionmaker() as s:
        outcomes = await expire_stale(s, svc.clock)
        await s.commit()
    if any(o.purpose == "verification" for o in outcomes):
        await svc.notifier.send(
            "Квиз остался без ответа — блок засчитан как сделанный, но без проверки."
        )
    for o in outcomes:
        if (
            o.purpose == "diagnostic"
        ):  # an unanswered question must not stall the diagnostic forever
            await maybe_finish(svc, o.quiz_id)


async def _reminders(svc: Services) -> None:
    async with svc.sessionmaker() as s:
        due = (
            await s.scalars(
                select(Reminder).where(
                    Reminder.status == "pending", Reminder.remind_at <= svc.clock.now()
                )
            )
        ).all()
        texts = [r.text for r in due]
        for r in due:
            r.status = "sent"
        await s.commit()
    for text in texts:
        await svc.notifier.send(f"🔔 {h(text)}")


async def _ladder(svc: Services) -> None:
    quiet = await quiet_range(svc)
    async with svc.sessionmaker() as s:
        await run_ladder(s, svc.clock, quiet)
        await s.commit()


async def _flush_outbox(svc: Services) -> None:
    await flush(svc.sessionmaker, svc.notifier, svc.clock, refresh=refresh_message)


async def _expire_proposals(svc: Services) -> None:
    async with svc.sessionmaker() as s:
        for p in (
            await s.scalars(
                select(Proposal).where(
                    Proposal.status == "pending", Proposal.expires_at < svc.clock.now()
                )
            )
        ).all():
            p.status = "expired"
        await s.commit()


async def morning(svc: Services) -> None:
    async with svc.sessionmaker() as s:
        digest_rows = await pending_digest(s)
        digest = [r.text for r in digest_rows]
        missed_checkin = await checkin.awaiting(s, svc.clock)
        planned = await has_plan(s)
        text = await morning_digest(s, svc.settings, svc.clock) if planned else ""
        pending = await s.scalar(  # only cards not yet shown (e.g. the nightly replan)
            select(Proposal.id)
            .where(
                Proposal.status == "pending",
                Proposal.kind == "plan",
                Proposal.tg_message_id.is_(None),
            )
            .order_by(Proposal.id.desc())
        )
    if digest:  # new files, new assignments… collected since yesterday's digest
        text = (text + "\n\n" if text else "") + "<b>Новое в Canvas</b>\n" + "\n".join(digest)
    if not planned:
        if text and await svc.notifier.send(text) is not None and digest_rows:
            await mark_digest_sent(svc.sessionmaker, [r.id for r in digest_rows], svc.clock)
        return
    if missed_checkin is not None and missed_checkin < svc.clock.today():
        await svc.notifier.send(
            "Вчера не было check-in. Коротко: что успел сделать? (можно голосом)", checkin_kb()
        )
    if await svc.notifier.send(text) is not None and digest_rows:  # mark only after delivery
        await mark_digest_sent(svc.sessionmaker, [r.id for r in digest_rows], svc.clock)
    await _morning_voice(svc)
    if pending:
        await send_proposal(svc, pending)


async def _morning_voice(svc: Services) -> None:
    if svc.voice is None:
        return
    async with svc.sessionmaker() as s:
        if not (await effective(s, svc.settings))["morning_voice"]:
            return
        brief = await morning_brief_text(s, svc.settings, svc.clock)
    try:
        audio, _ = await svc.voice.speak(brief)
    except Exception:  # the text digest was already sent
        log.exception("Morning voice brief failed")
        return
    if audio:
        await svc.notifier.send_voice(audio, caption="🎙 Утренний бриф")


async def evening(svc: Services) -> None:
    async with svc.sessionmaker() as s:
        if not await has_plan(s):
            return
        await checkin.ask(s, svc.clock.today())
        await s.commit()
        today = await day_view(s, svc.settings, svc.clock.today())
    await svc.notifier.send(
        f"🌙 Check-in: что сделал сегодня? Можно текстом или голосом.\n\n{today}", checkin_kb()
    )


async def weekly(svc: Services) -> None:
    async with svc.sessionmaker() as s:
        if not await has_plan(s):
            return
        text = await weekly_report(s, svc.settings, svc.clock)
    await svc.notifier.send(text)


async def nightly(svc: Services) -> None:
    """Recompute from actual progress; the result waits as a proposal for the morning digest."""
    async with svc.sessionmaker() as s:
        if not await has_plan(s):
            return
    await propose_replan(svc, "Ночной пересчёт по фактическому прогрессу", send=False)
