"""Evening check-in (SPEC §6 Enforcement): «что сделал сегодня?» → parsed into sessions.

The prompt is a template; only the free-form answer is parsed, by Haiku with a JSON schema and
the `checkin_parse` budget. If the answer is not a report, the message goes to the normal agent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.brain.accounting import local_day_start
from agent.brain.llm import LlmClient, text_of
from agent.core.clock import Clock
from agent.db.models import Block, Checkin, Topic
from agent.planner.blocks import record_study, set_block_status
from agent.planner.render import KIND_RU

CHECKIN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_report": {"type": "boolean"},
        "done_block_ids": {"type": "array", "items": {"type": "integer"}},
        "skipped_block_ids": {"type": "array", "items": {"type": "integer"}},
        "extra": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic_id": {"type": "integer"},
                    "minutes": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["learn", "practice", "review", "mock"]},
                },
                "required": ["topic_id", "minutes", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_report", "done_block_ids", "skipped_block_ids", "extra"],
    "additionalProperties": False,
}

SYSTEM = (
    "Parse a student's evening study report (Russian, may be a voice transcript). Map it onto today's "
    "planned blocks and known topics by id. is_report=false if the message is not about what they "
    "studied today. Only include ids from the lists. Minutes: integers."
)


async def ask(session: AsyncSession, day: date) -> None:
    row = await session.get(Checkin, day) or Checkin(date_local=day)
    if row.status != "done":
        row.status = "asked"
    session.add(row)
    await session.flush()


async def awaiting(session: AsyncSession, clock: Clock) -> date | None:
    """The check-in date we are waiting for (today, or yesterday if it was skipped)."""
    today = clock.today()
    for d in (today, today - timedelta(days=1)):
        row = await session.get(Checkin, d)
        if row and row.status == "asked":
            return d
    return None


@dataclass(frozen=True, slots=True)
class CheckinOutcome:
    is_report: bool
    done: int = 0
    skipped: int = 0
    extra_minutes: int = 0
    summary: str = ""


@dataclass(frozen=True, slots=True)
class _Prompt:
    text: str
    block_ids: frozenset[int]  # only ids shown to the model may be changed
    topic_ids: frozenset[int]


_REPORT_HINT = re.compile(
    r"(сделал|успел|прош[её]л|решил|прочитал|читал|повторил|учил|разобрал|не успел|пропустил|"
    r"минут|\d+\s*(ч|час)|сегодня|блок|задач|лекци|ничего)",
    re.IGNORECASE,
)


def looks_like_report(text: str) -> bool:
    """Cheap pre-filter so ordinary chat during an open check-in doesn't cost an extra LLM call."""
    return bool(_REPORT_HINT.search(text))


async def _prompt(session: AsyncSession, clock: Clock, day: date, text: str) -> _Prompt:
    start = local_day_start(clock) - timedelta(days=(clock.today() - day).days)
    blocks = (
        await session.scalars(
            select(Block)
            .where(Block.starts_at >= start, Block.starts_at < start + timedelta(days=1))
            .order_by(Block.starts_at)
        )
    ).all()
    topics = (await session.scalars(select(Topic).where(Topic.status == "active"))).all()
    names = {t.id: t.name for t in topics}
    zone = clock.zone
    lines = ["Today's blocks:"] + [
        f"{b.id}: {b.starts_at.astimezone(zone):%H:%M} {names.get(b.topic_id, 'mock')} ({b.kind}) [{b.status}]"
        for b in blocks
    ]
    lines += ["Topics:"] + [f"{t.id}: {t.name}" for t in topics[:60]]
    lines += ["Report:", text[:1500]]
    return _Prompt(
        "\n".join(lines),
        frozenset(b.id for b in blocks),
        frozenset(t.id for t in topics[:60]),
    )


async def handle_answer(
    sessionmaker: async_sessionmaker, llm: LlmClient, clock: Clock, day: date, text: str
) -> CheckinOutcome:
    """Parse and apply a check-in answer. No DB transaction is held during the LLM call."""
    async with sessionmaker() as session:
        prompt = await _prompt(session, clock, day, text)
    res = await llm.call(
        "checkin_parse",
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt.text}],
        json_schema=CHECKIN_SCHEMA,
    )
    try:
        data = json.loads(text_of(res.message))
    except ValueError:
        return CheckinOutcome(False)
    if not data.get("is_report"):
        return CheckinOutcome(False)
    async with sessionmaker() as session:
        outcome = await _apply(session, clock, day, text, data, prompt)
        await session.commit()
    return outcome


async def _apply(
    session: AsyncSession,
    clock: Clock,
    day: date,
    text: str,
    data: dict[str, Any],
    prompt: _Prompt,
) -> CheckinOutcome:
    done = skipped = extra_min = 0
    for bid in data.get("done_block_ids", []):
        if bid not in prompt.block_ids:  # never trust ids the model was not shown
            continue
        block = await session.get(Block, bid)
        if block and block.status in ("planned", "started", "skipped"):
            await set_block_status(session, bid, "done", clock)
            done += 1
    for bid in data.get("skipped_block_ids", []):
        if bid not in prompt.block_ids:
            continue
        block = await session.get(Block, bid)
        if block and block.status in ("planned", "started"):
            await set_block_status(session, bid, "skipped", clock)
            skipped += 1
    for item in data.get("extra", [])[:10]:
        if item.get("topic_id") not in prompt.topic_ids:
            continue
        topic = await session.get(Topic, item["topic_id"])
        minutes = int(item["minutes"])
        if topic and 5 <= minutes <= 600 and item["kind"] in KIND_RU:
            await record_study(session, topic, minutes, item["kind"], clock=clock, source="checkin")
            extra_min += minutes
    row = await session.get(Checkin, day) or Checkin(date_local=day)
    row.status, row.raw_text, row.parsed_json, row.completed_at = (
        "done",
        text[:2000],
        data,
        clock.now(),
    )
    session.add(row)
    await session.flush()
    summary = (
        f"Записал: выполнено блоков {done}, пропущено {skipped}"
        + (f", ещё {extra_min} мин сверх плана" if extra_min else "")
        + "."
    )
    return CheckinOutcome(True, done, skipped, extra_min, summary)
