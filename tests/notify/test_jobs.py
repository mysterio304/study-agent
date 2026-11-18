"""Scheduled jobs and proposal flows with a recording notifier (no Telegram, no LLM)."""

from datetime import date, time, timedelta

import pytest
from sqlalchemy import func, select

from agent.canvas.fake import FakeCanvasSource
from agent.config import Settings
from agent.core.clock import FixedClock
from agent.core.timeutil import local_to_utc
from agent.db.models import Block, Checkin, Proposal, Reminder, Setting, Topic
from agent.flows import Services, apply_choice, continue_setup, propose_replan
from agent.notify import jobs
from agent.prep.seed import seed_exams
from tests.planner.conftest import HKT, at


class Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, object]] = []

    async def send(self, text: str, kb: object = None) -> int:
        self.messages.append((text, kb))
        return len(self.messages)

    async def send_voice(self, audio: bytes, caption: str | None = None) -> int:
        self.messages.append((f"[voice] {caption or ''}", None))
        return len(self.messages)

    async def send_poll(
        self, question: str, options: list[str], correct: int, explanation: str
    ) -> str:
        self.messages.append((f"[poll] {question}", None))
        return f"poll-{len(self.messages)}"

    def texts(self) -> list[str]:
        return [t for t, _ in self.messages]


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(at(date(2026, 11, 19), 7, 0), HKT)


@pytest.fixture
async def svc(sessionmaker, clk):
    async with sessionmaker() as s:
        await seed_exams(s)
        await s.flush()
        for exam_id, names in {
            1: ["TCP", "Routing"],
            2: ["DP"],
            3: ["Eigen"],
            4: ["Features"],
        }.items():
            for n in names:
                s.add(
                    Topic(
                        exam_id=exam_id,
                        name=n,
                        effort_hours=3,
                        importance=2,
                        level=1,
                        target_level=2,
                        status="active",
                    )
                )
        await s.commit()
    return Services(Settings(_env_file=None), sessionmaker, clk, FakeCanvasSource(), Recorder())


async def _with_plan(svc: Services) -> None:
    pid = await propose_replan(svc, "Первый план")
    assert pid is not None
    assert "обновлён" in await apply_choice(svc, pid, "A")


def _set(clk: FixedClock, hh: int, mm: int = 0, day_offset: int = 0) -> None:
    target = local_to_utc(date(2026, 11, 19) + timedelta(days=day_offset), time(hh, mm), HKT)
    clk.advance(seconds=(target - clk.now()).total_seconds())


async def test_setup_completion_sends_first_plan_proposal(svc) -> None:
    await continue_setup(svc)
    texts = svc.notifier.texts()
    assert any("Предложение #" in t for t in texts)
    async with svc.sessionmaker() as s:
        assert await s.scalar(select(func.count(Proposal.id))) == 1


async def test_ping_reping_and_gentle_escalation(svc, clk) -> None:
    await _with_plan(svc)
    _set(clk, 9, 0)
    await jobs.tick(svc)
    _set(clk, 9, 5)
    await jobs.tick(svc)  # too early for a re-ping
    _set(clk, 9, 15)
    await jobs.tick(svc)
    _set(clk, 9, 30)
    await jobs.tick(svc)
    _set(clk, 9, 45)
    await jobs.tick(svc)  # max 3 pings
    pings = [
        t
        for t, kb in svc.notifier.messages
        if kb is not None and any(b.text == "▶️ Начал" for row in kb.inline_keyboard for b in row)
    ]
    assert len(pings) == 3
    assert "Начинаем?" in pings[0] and "15 минут" in pings[1] and "не состоялся" in pings[2]


async def test_only_one_auto_move_per_day_rest_is_debt(svc, clk) -> None:
    await _with_plan(svc)
    _set(clk, 13, 0)  # several morning blocks missed at once
    await jobs.tick(svc)
    _set(clk, 16, 0)
    await jobs.tick(svc)
    moves = [t for t in svc.notifier.texts() if "перенёс" in t]
    assert len(moves) == 1
    async with svc.sessionmaker() as s:
        assert await s.scalar(select(func.count(Block.id)).where(Block.status == "moved")) == 1
        assert await s.scalar(select(func.count(Block.id)).where(Block.status == "skipped")) >= 2


async def test_started_block_gets_end_question(svc, clk) -> None:
    await _with_plan(svc)
    async with svc.sessionmaker() as s:
        b = await s.scalar(select(Block).order_by(Block.starts_at))
        b.status = "started"
        await s.commit()
        end = b.ends_at
    clk.advance(seconds=(end - clk.now()).total_seconds() + 60)
    await jobs.tick(svc)
    await jobs.tick(svc)  # asked only once
    assert sum("Как прошло?" in t for t in svc.notifier.texts()) == 1


async def test_due_reminders_are_sent_once(svc, clk) -> None:
    async with svc.sessionmaker() as s:
        s.add(
            Reminder(
                text="Купить кофе",
                remind_at=clk.now() + timedelta(minutes=5),
                status="pending",
                source="user",
                created_at=clk.now(),
            )
        )
        await s.commit()
    await jobs.tick(svc)
    clk.advance(minutes=6)
    await jobs.tick(svc)
    await jobs.tick(svc)
    assert sum("Купить кофе" in t for t in svc.notifier.texts()) == 1


async def test_evening_checkin_and_morning_followup(svc, clk) -> None:
    await _with_plan(svc)
    _set(clk, 21, 30)
    await jobs.evening(svc)
    assert "Check-in" in svc.notifier.texts()[-1]
    async with svc.sessionmaker() as s:
        assert (await s.get(Checkin, date(2026, 11, 19))).status == "asked"
    _set(clk, 8, 30, day_offset=1)
    await jobs.morning(svc)
    texts = svc.notifier.texts()
    assert any("Вчера не было check-in" in t for t in texts)
    assert any("Доброе утро" in t for t in texts)


async def test_nightly_replan_waits_for_morning(svc, clk) -> None:
    await _with_plan(svc)
    _set(clk, 13, 0)
    await jobs.tick(svc)  # missed blocks → progress differs from plan
    sent_before = len(svc.notifier.messages)
    _set(clk, 3, 30, day_offset=1)
    await jobs.nightly(svc)
    assert len(svc.notifier.messages) == sent_before  # nothing sent at night
    _set(clk, 8, 30, day_offset=1)
    await jobs.morning(svc)
    assert any("Ночной пересчёт" in t for t in svc.notifier.texts()[sent_before:])


async def test_settings_proposal_applies_and_triggers_replan(svc) -> None:
    await _with_plan(svc)
    async with svc.sessionmaker() as s:
        p = Proposal(
            created_at=svc.clock.now(),
            kind="settings",
            summary_ru="Лимит: 5",
            options_json=[{"key": "A", "setting": "cap_hours", "value": 5.0}, {"key": "KEEP"}],
            status="pending",
            expires_at=svc.clock.now() + timedelta(hours=5),
        )
        s.add(p)
        await s.commit()
    assert await apply_choice(svc, p.id, "A") == "Настройка применена."
    async with svc.sessionmaker() as s:
        assert (await s.get(Setting, "cap_hours")).value_json == 5.0
        assert (
            await s.scalar(select(func.count(Proposal.id)).where(Proposal.status == "pending")) == 1
        )
    assert await apply_choice(svc, p.id, "A") == "Это предложение уже неактуально."


async def test_expired_proposals_are_marked(svc, clk) -> None:
    await propose_replan(svc, "x", send=False)
    clk.advance(hours=40)
    await jobs.tick(svc)
    async with svc.sessionmaker() as s:
        assert (
            await s.scalar(select(func.count(Proposal.id)).where(Proposal.status == "expired")) == 1
        )
