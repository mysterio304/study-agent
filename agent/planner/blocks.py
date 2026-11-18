"""Block lifecycle: start/done/skip, missed-block handling, debt, and topic progress."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.core.clock import Clock
from agent.db.models import Block, StudySession, Topic
from agent.planner.model import PlannedBlock
from agent.planner.service import (
    PlanInputs,
    current_version,
    future_blocks,
    get_overrides,
    write_version,
)
from agent.planner.slots import day_slots
from agent.planner.validators import validate_plan

REVIEW_INTERVALS_DAYS = (1, 3, 7, 14)
MISSED_GRACE = timedelta(minutes=30)
DEBT_WINDOW = timedelta(days=7)


class BlockError(RuntimeError):
    pass


async def record_study(
    session: AsyncSession,
    topic: Topic | None,
    minutes: int,
    kind: str,
    *,
    clock: Clock,
    source: str,
    block_id: int | None = None,
    note: str | None = None,
    started_at: datetime | None = None,
) -> StudySession:
    """Log a session and advance the topic's progress and spaced-repetition schedule."""
    now = clock.now()
    session_row = StudySession(
        topic_id=topic.id if topic else None,
        block_id=block_id,
        started_at=started_at,
        ended_at=now,
        minutes=minutes,
        kind=kind,
        verified=False,
        source=source,
        note=note,
        created_at=now,
    )
    session.add(session_row)
    if topic is not None:
        if kind in ("learn", "practice"):
            topic.progress_min = (topic.progress_min or 0) + minutes
        step = min(topic.review_step or 0, len(REVIEW_INTERVALS_DAYS) - 1)
        topic.last_studied_at = now
        topic.next_review_at = now + timedelta(days=REVIEW_INTERVALS_DAYS[step])
        topic.review_step = (topic.review_step or 0) + 1
    await session.flush()
    return session_row


async def set_block_status(
    session: AsyncSession, block_id: int, status: str, clock: Clock
) -> Block:
    block = await session.get(Block, block_id)
    if block is None:
        raise BlockError("Блок не найден.")
    if block.status in ("done", "moved"):
        return block  # idempotent: repeated button presses are harmless
    if status == "started":
        block.status = "started"
    elif status == "done":
        block.status = "done"
        topic = await session.get(Topic, block.topic_id) if block.topic_id else None
        minutes = int((block.ends_at - block.starts_at).total_seconds() // 60)
        await record_study(
            session,
            topic,
            minutes,
            block.kind,
            clock=clock,
            source="block",
            block_id=block.id,
            started_at=block.starts_at,
        )
    elif status == "skipped":
        block.status = "skipped"
    else:
        raise BlockError(f"Unknown status {status}")
    await session.flush()
    return block


async def mark_missed_blocks(session: AsyncSession, clock: Clock) -> list[Block]:
    """Blocks whose end passed (+grace) without being started become 'skipped' (→ debt)."""
    cutoff = clock.now() - MISSED_GRACE
    rows = list(
        await session.scalars(
            select(Block).where(Block.status == "planned", Block.ends_at < cutoff)
        )
    )
    for b in rows:
        b.status = "skipped"
    await session.flush()
    return rows


@dataclass(frozen=True, slots=True)
class Debt:
    blocks: int
    hours: float


async def debt(session: AsyncSession, clock: Clock) -> Debt:
    """Skipped blocks in the last 7 days whose topic was not studied afterwards."""
    since = clock.now() - DEBT_WINDOW
    skipped = list(
        await session.scalars(
            select(Block).where(Block.status == "skipped", Block.starts_at >= since)
        )
    )
    open_items = []
    for b in skipped:
        if b.topic_id is None:
            open_items.append(b)
            continue
        topic = await session.get(Topic, b.topic_id)
        if topic is None or topic.last_studied_at is None or topic.last_studied_at < b.ends_at:
            open_items.append(b)
    minutes = sum((b.ends_at - b.starts_at).total_seconds() / 60 for b in open_items)
    return Debt(len(open_items), round(minutes / 60, 1))


async def auto_move_missed(
    session: AsyncSession, block_id: int, inputs: PlanInputs, clock: Clock
) -> Block | None:
    """small_auto: move ONE missed/declined block to the next free slot today or tomorrow.
    Records a plan version (proposed_by='auto'). Returns the new block or None if no slot."""
    block = await session.get(Block, block_id)
    if block is None or block.status not in ("planned", "skipped"):
        return None
    now = clock.now()
    taken = await future_blocks(session, now)
    today = clock.today()
    baseline = len(validate_plan(taken, inputs.exams, inputs.topics, inputs.cfg, busy=inputs.busy))
    for day in (today, today + timedelta(days=1)):
        for slot in day_slots(day, inputs.cfg, inputs.exams, inputs.busy, not_before=now):
            if any(t.start < slot.end and slot.start < t.end for t in taken):
                continue
            moved = PlannedBlock(slot.start, slot.end, block.exam_id, block.topic_id, block.kind)  # type: ignore[arg-type]
            candidate = sorted([*taken, moved], key=lambda b: b.start)
            # The move must not break a non-negotiable (e.g. a learn block onto an exam's eve).
            if (
                len(
                    validate_plan(
                        candidate, inputs.exams, inputs.topics, inputs.cfg, busy=inputs.busy
                    )
                )
                > baseline
            ):
                continue
            version = await write_version(
                session,
                candidate,
                clock,
                reason=f"Автоперенос пропущенного блока #{block.id}",
                proposed_by="auto",
                overrides=await get_overrides(session),
            )
            new_block = await session.scalar(
                select(Block).where(
                    Block.plan_version_id == version.id, Block.starts_at == slot.start
                )
            )
            block.status = "moved"
            block.moved_to_block_id = new_block.id if new_block else None
            await session.flush()
            return new_block
    return None


async def has_plan(session: AsyncSession) -> bool:
    return await current_version(session) is not None
