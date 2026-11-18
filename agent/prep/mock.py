"""Mock exam mode (SPEC §6): «начинаю past paper» → timer for the real exam length → score and the
topics where marks were lost → weak topics drop a level and get more blocks at the next replan.

States: running → awaiting_score → awaiting_topics → done (or cancelled).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.brain.accounting import local_day_start
from agent.core.clock import Clock
from agent.db.models import Block, Exam, MockExam, StudySession, Topic

TRIGGER_RE = re.compile(
    r"(начинаю\s+(past\s*paper|мок|пробник|пробный)|mock\s*exam|start(ing)?\s+(a\s+)?past\s*paper)",
    re.IGNORECASE,
)
# Only a bare score counts ("72", "72%", "36/50", "36 из 50", "~72", "примерно 72") — anything else in
# chat goes to the agent as usual, so "перенеси на 2" is never mistaken for a 2 % mock.
SCORE_RE = re.compile(
    r"^\s*(?:~|около|примерно|где-то)?\s*(\d{1,3}(?:[.,]\d+)?)\s*"
    r"(%|(?:/|из|out of)\s*(\d{1,3}))?\s*[.!]?\s*$",
    re.IGNORECASE,
)
LAST_PING_BEFORE = timedelta(minutes=10)
ANSWER_WINDOW = timedelta(hours=2)  # unanswered score/topics questions expire (mock cancelled)
ACTIVE = ("running", "awaiting_score", "awaiting_topics")


def is_trigger(text: str) -> bool:
    return bool(TRIGGER_RE.search(text))


def parse_score(text: str) -> float | None:
    """'72', '72%', '36/50', '36 / 50' → percentage; None if no plausible score."""
    m = SCORE_RE.match(text)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    if m.group(3):
        total = float(m.group(3))
        return round(100 * value / total, 1) if total > 0 and value <= total else None
    return value if 0 <= value <= 100 else None


async def active_mock(session: AsyncSession) -> MockExam | None:
    return await session.scalar(
        select(MockExam).where(MockExam.state.in_(ACTIVE)).order_by(MockExam.id.desc())
    )


async def start_mock(
    session: AsyncSession, exam_id: int, file_id: int | None, clock: Clock
) -> MockExam:
    exam = await session.get(Exam, exam_id)
    if exam is None:
        raise ValueError("unknown exam")
    for old in (await session.scalars(select(MockExam).where(MockExam.state.in_(ACTIVE)))).all():
        old.state = "cancelled"  # only one mock at a time
    now = clock.now()
    mock = MockExam(
        exam_id=exam_id,
        file_id=file_id,
        started_at=now,
        ends_at=now + timedelta(minutes=exam.duration_min),
        state="running",
        pings_sent=0,
    )
    session.add(mock)
    await session.flush()
    return mock


@dataclass(frozen=True, slots=True)
class MockPing:
    mock_id: int
    kind: str  # half | last10 | end


async def due_pings(session: AsyncSession, clock: Clock) -> list[MockPing]:
    now, out = clock.now(), []
    for m in (await session.scalars(select(MockExam).where(MockExam.state == "running"))).all():
        half = m.started_at + (m.ends_at - m.started_at) / 2
        if now >= m.ends_at:
            m.state, m.pings_sent = "awaiting_score", 3
            out.append(MockPing(m.id, "end"))
        elif m.pings_sent < 2 and now >= m.ends_at - LAST_PING_BEFORE:
            m.pings_sent = 2
            out.append(MockPing(m.id, "last10"))
        elif m.pings_sent < 1 and now >= half:
            m.pings_sent = 1
            out.append(MockPing(m.id, "half"))
    await session.flush()
    return out


async def expire_unanswered(session: AsyncSession, clock: Clock) -> int:
    """Mocks waiting for a score/topics longer than ANSWER_WINDOW are cancelled."""
    stale = (
        await session.scalars(
            select(MockExam).where(
                MockExam.state.in_(("awaiting_score", "awaiting_topics")),
                MockExam.ends_at < clock.now() - ANSWER_WINDOW,
            )
        )
    ).all()
    for m in stale:
        m.state = "cancelled"
    await session.flush()
    return len(stale)


async def finish_early(session: AsyncSession, mock_id: int, clock: Clock) -> MockExam | None:
    m = await session.get(MockExam, mock_id)
    if m is None or m.state != "running":
        return None
    m.state, m.ends_at = "awaiting_score", min(m.ends_at, clock.now())
    await session.flush()
    return m


async def record_score(session: AsyncSession, mock: MockExam, score: float) -> None:
    mock.score_pct, mock.state, mock.lost_topics_json = score, "awaiting_topics", []
    await session.flush()


async def toggle_topic(session: AsyncSession, mock_id: int, topic_id: int) -> list[int]:
    m = await session.get(MockExam, mock_id)
    if m is None or m.state != "awaiting_topics":
        return []
    current = list(m.lost_topics_json or [])
    m.lost_topics_json = (
        [t for t in current if t != topic_id] if topic_id in current else [*current, topic_id]
    )
    await session.flush()
    return m.lost_topics_json


async def finish(
    session: AsyncSession, mock_id: int, clock: Clock
) -> tuple[MockExam, list[str]] | None:
    """Apply results: lost topics −1 level (min 0) and due for review now; log the session; mark
    today's planned mock blocks of this exam done. Returns (mock, names of weakened topics)."""
    m = await session.get(MockExam, mock_id)
    if m is None or m.state != "awaiting_topics":
        return None
    weakened = []
    for tid in m.lost_topics_json or []:
        topic = await session.get(Topic, tid)
        if topic is None or topic.exam_id != m.exam_id:
            continue
        topic.level = max(0, topic.level - 1)
        topic.progress_min, topic.level_changed_at, topic.next_review_at = (
            0,
            clock.now(),
            clock.now(),
        )
        weakened.append(topic.name)
    minutes = int((m.ends_at - m.started_at).total_seconds() // 60)
    session.add(
        StudySession(
            topic_id=None,
            started_at=m.started_at,
            ended_at=m.ends_at,
            minutes=minutes,
            kind="mock",
            verified=True,
            quiz_score=(m.score_pct or 0) / 100,
            source="mock",
            note=f"mock exam {m.id}",
            created_at=clock.now(),
        )
    )
    day_start = local_day_start(clock)
    for b in (
        await session.scalars(
            select(Block).where(
                Block.exam_id == m.exam_id,
                Block.kind == "mock",
                Block.status.in_(("planned", "started")),
                Block.starts_at >= day_start,
                Block.starts_at < day_start + timedelta(days=1),
            )
        )
    ).all():
        b.status = "done"  # the mock session above already logs the time (no second StudySession)
    m.state, m.finished_at = "done", clock.now()
    await session.flush()
    return m, weakened
