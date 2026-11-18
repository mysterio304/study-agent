"""Plan proposals end-to-end against a real (temporary) SQLite DB."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from agent.config import Settings
from agent.core.clock import FixedClock
from agent.db.models import Block, PlanVersion, Proposal, Topic
from agent.planner.blocks import auto_move_missed, debt, mark_missed_blocks, set_block_status
from agent.planner.service import (
    PlanError,
    apply_proposal,
    future_blocks,
    load_inputs,
    propose_plan,
    propose_undo,
)
from agent.prep.seed import seed_exams
from tests.planner.conftest import HKT, at

SETTINGS = Settings(_env_file=None)


@pytest.fixture
async def db(sessionmaker):
    async with sessionmaker() as s:
        await seed_exams(s)
        await s.flush()
        for exam_id, names in {
            1: ["TCP", "Routing", "Link layer"],
            2: ["DP", "Graphs"],
            3: ["Eigenvalues"],
            4: ["Features"],
        }.items():
            for i, n in enumerate(names):
                s.add(
                    Topic(
                        exam_id=exam_id,
                        name=n,
                        effort_hours=3,
                        importance=3 - i % 2,
                        level=1,
                        target_level=2,
                        status="active",
                    )
                )
        await s.commit()
    return sessionmaker


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(at(date(2026, 11, 19), 8, 0), HKT)


async def _propose_and_apply(db, clk, reason="Первый план"):
    async with db() as s:
        inputs = await load_inputs(s, SETTINGS, clk)
        p = await propose_plan(s, inputs, clk, reason=reason, proposed_by="planner")
        await s.commit()
    async with db() as s:
        await apply_proposal(s, p.id, "A", clk)
        await s.commit()
    return p


async def test_proposal_applies_only_on_confirmation(db, clk) -> None:
    async with db() as s:
        inputs = await load_inputs(s, SETTINGS, clk)
        p = await propose_plan(s, inputs, clk, reason="Первый план", proposed_by="planner")
        await s.commit()
        assert p is not None and [o["key"] for o in p.options_json] == ["A", "KEEP"]
        assert await s.scalar(select(func.count(Block.id))) == 0  # nothing applied yet
    async with db() as s:
        version = await apply_proposal(s, p.id, "A", clk)
        await s.commit()
    async with db() as s:
        assert version is not None
        assert await s.scalar(select(func.count(Block.id))) > 50
        assert (await s.get(Proposal, p.id)).status == "applied"


async def test_identical_replan_returns_none_and_keep_rejects(db, clk) -> None:
    await _propose_and_apply(db, clk)
    async with db() as s:
        inputs = await load_inputs(s, SETTINGS, clk)
        assert await propose_plan(s, inputs, clk, reason="nightly", proposed_by="planner") is None
        p = await propose_plan(
            s,
            inputs,
            clk,
            reason="пауза",
            proposed_by="agent",
            changes={"paused_until": {1: "2026-11-25"}},
        )
        await s.commit()
    async with db() as s:
        assert await apply_proposal(s, p.id, "KEEP", clk) is None
        await s.commit()
        assert (await s.get(Proposal, p.id)).status == "rejected"
        with pytest.raises(PlanError):
            await apply_proposal(s, p.id, "A", clk)


async def test_pause_change_with_compromise_and_undo(db, clk) -> None:
    await _propose_and_apply(db, clk)
    async with db() as s:
        inputs = await load_inputs(s, SETTINGS, clk)
        p = await propose_plan(
            s,
            inputs,
            clk,
            reason="Не трогать Networks до 25 ноя",
            proposed_by="agent",
            changes={"paused_until": {1: "2026-11-25"}},
            alt_changes={"paused_until": {1: "2026-11-22"}},
        )
        await s.commit()
        assert [o["key"] for o in p.options_json] == ["A", "B", "KEEP"]
        assert "Networks" in p.options_json[0]["diff"]
    async with db() as s:
        await apply_proposal(s, p.id, "A", clk)
        await s.commit()
    async with db() as s:
        blocks = await future_blocks(s, clk.now())
        assert not any(b.exam_id == 1 and b.start < at(date(2026, 11, 25), 0) for b in blocks)
        inputs = await load_inputs(s, SETTINGS, clk)
        assert inputs.cfg.paused_until[1] == date(2026, 11, 25)
        undo = await propose_undo(s, inputs, clk)
        await s.commit()
    async with db() as s:
        await apply_proposal(s, undo.id, "A", clk)
        await s.commit()
    async with db() as s:
        blocks = await future_blocks(s, clk.now())
        assert any(b.exam_id == 1 and b.start < at(date(2026, 11, 25), 0) for b in blocks)
        assert (await load_inputs(s, SETTINGS, clk)).cfg.paused_until == {}
        assert await s.scalar(select(func.count(PlanVersion.id))) == 3


async def test_expired_proposal_cannot_be_applied(db, clk) -> None:
    async with db() as s:
        inputs = await load_inputs(s, SETTINGS, clk)
        p = await propose_plan(s, inputs, clk, reason="x", proposed_by="planner")
        await s.commit()
    clk.advance(hours=48)
    async with db() as s:
        with pytest.raises(PlanError, match="истёк"):
            await apply_proposal(s, p.id, "A", clk)


async def test_done_block_logs_session_and_progress(db, clk) -> None:
    await _propose_and_apply(db, clk)
    async with db() as s:
        first = await s.scalar(select(Block).order_by(Block.starts_at))
        clk.advance(hours=2)
        await set_block_status(s, first.id, "done", clk)
        await set_block_status(s, first.id, "done", clk)  # idempotent
        await s.commit()
        topic = await s.get(Topic, first.topic_id)
        assert topic.last_studied_at == clk.now()
        assert topic.next_review_at == clk.now() + timedelta(days=1)
        if first.kind in ("learn", "practice"):
            assert topic.progress_min == 50


async def test_missed_blocks_become_debt_and_one_can_auto_move(db, clk) -> None:
    await _propose_and_apply(db, clk)
    clk.advance(hours=4)  # 12:00 → the 09:00–11:50 blocks are past
    async with db() as s:
        missed = await mark_missed_blocks(s, clk)
        await s.commit()
        assert len(missed) >= 2
        d = await debt(s, clk)
        assert d.blocks == len(missed) and d.hours > 0
        inputs = await load_inputs(s, SETTINGS, clk)
        moved = await auto_move_missed(s, missed[0].id, inputs, clk)
        await s.commit()
        assert moved is not None and moved.starts_at > clk.now()
        assert (await s.get(Block, missed[0].id)).status == "moved"
        latest = await s.scalar(select(PlanVersion).order_by(PlanVersion.id.desc()))
        assert latest.proposed_by == "auto"
