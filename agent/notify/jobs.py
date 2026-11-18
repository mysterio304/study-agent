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
from agent.notify.recheck import refresh_message
from agent.notify.report import weekly_report
from agent.planner.blocks import auto_move_missed, has_plan, mark_missed_blocks
from agent.planner.render import fmt_day
from agent.planner.service import load_inputs
from agent.prep import checkin
from agent.telegram.cards import block_ping, day_view, morning_digest
from agent.telegram.format import h
from agent.telegram.keyboards import block_end_kb, block_start_kb, checkin_kb

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
            to_send.append((b.id, f"Блок закончился: {h(topic)}. Как прошло?"))
            b.ping_count = END_ASKED
        await s.commit()
    for block_id, text in to_send:
        await svc.notifier.send(text, block_end_kb(block_id))


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
    if pending:
        await send_proposal(svc, pending)


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
