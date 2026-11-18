"""Regression tests for findings from the Milestone 2 code review."""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from agent.brain.batches import BatchRunner, Purpose
from agent.brain.llm import LlmClient
from agent.brain.tool_handlers import ToolContext, find_topic
from agent.brain.tools import run_tool
from agent.canvas.fake import FakeCanvasSource
from agent.config import Settings
from agent.core.clock import FixedClock
from agent.db.models import Block, LlmBatch, LlmUsage, PlanVersion, Proposal, Topic
from agent.flows import Services, apply_choice, propose_replan
from agent.notify import jobs
from agent.planner.blocks import auto_move_missed
from agent.planner.service import load_inputs
from agent.prep import checkin, topics_service
from agent.prep.seed import seed_exams
from agent.telegram.chat import safe_html
from tests.brain.conftest import FakeBackend, text_msg
from tests.notify.test_jobs import Recorder
from tests.planner.conftest import HKT, at

SETTINGS = Settings(_env_file=None)


@pytest.fixture
async def db(sessionmaker):
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
    return sessionmaker


async def test_auto_move_never_lands_a_learn_block_on_an_exam_eve(db) -> None:
    # Dec 6, 21:00: tomorrow (Dec 7) is the eve of Networks → only review blocks allowed there.
    clk = FixedClock(at(date(2026, 12, 6), 21, 0), HKT)
    svc = Services(SETTINGS, db, clk, FakeCanvasSource(), Recorder())
    await apply_choice(svc, await propose_replan(svc, "x", send=False), "A")
    async with db() as s:
        v = await s.scalar(select(PlanVersion.id))
        missed = Block(
            plan_version_id=v,
            starts_at=at(date(2026, 12, 6), 18),
            exam_id=2,
            ends_at=at(date(2026, 12, 6), 18, 50),
            topic_id=3,
            kind="learn",
            status="skipped",
        )
        s.add(missed)
        await s.commit()
        inputs = await load_inputs(s, SETTINGS, clk)
        moved = await auto_move_missed(s, missed.id, inputs, clk)
        assert moved is None or moved.starts_at.astimezone(HKT).date() != date(2026, 12, 7)


async def test_batch_handler_failure_does_not_rebill(db) -> None:
    clk = FixedClock(at(date(2026, 11, 19), 10), HKT)
    llm = LlmClient(FakeBackend(), db, SETTINGS, clk)

    class Ended:
        async def create(self, *, requests):
            return SimpleNamespace(id="b1")

        async def retrieve(self, batch_id):
            return SimpleNamespace(processing_status="ended")

        async def cancel(self, batch_id):
            return None

        async def results(self, batch_id):
            async def gen():
                msg = SimpleNamespace(
                    model="claude-haiku-5-5",
                    content=[],
                    usage=SimpleNamespace(input_tokens=100, output_tokens=10),
                )
                yield SimpleNamespace(
                    custom_id="x", result=SimpleNamespace(type="succeeded", message=msg)
                )

            return gen()

    async def boom(row, results):
        raise RuntimeError("handler failed")

    runner = BatchRunner(Ended(), llm, db, SETTINGS, clk)
    runner.register("p", Purpose(boom))
    async with db() as s:
        s.add(
            LlmBatch(
                anthropic_batch_id="b1",
                purpose="p",
                status="submitted",
                request_count=1,
                submitted_at=clk.now(),
            )
        )
        await s.commit()
    await runner.poll()
    await runner.poll()
    async with db() as s:
        assert await s.scalar(select(func.count(LlmUsage.id))) == 1
        assert (await s.scalar(select(LlmBatch))).status == "consumed"


async def test_discovery_skips_exams_with_a_batch_in_flight(db) -> None:
    from agent.prep.discovery import TopicDiscovery

    clk = FixedClock(at(date(2026, 11, 19), 10), HKT)
    async with db() as s:
        for t in (await s.scalars(select(Topic))).all():
            await s.delete(t)
        s.add(
            LlmBatch(
                anthropic_batch_id="b1",
                purpose="topic_discovery",
                status="submitted",
                request_count=4,
                meta_json={"exam_ids": [1, 2, 3, 4]},
                submitted_at=clk.now(),
            )
        )
        await s.commit()

    async def noop(eid: int) -> None:
        return None

    d = TopicDiscovery(db, FakeCanvasSource(), None, None, clk, noop)
    assert await d.in_progress()
    assert await d.start() == []


async def test_checkin_ignores_ids_not_shown_to_the_model(db) -> None:
    clk = FixedClock(at(date(2026, 11, 19), 21, 30), HKT)
    async with db() as s:
        v = PlanVersion(
            created_at=clk.now(),
            reason="t",
            proposed_by="planner",
            applied_at=clk.now(),
            snapshot_json={"blocks": []},
        )
        s.add(v)
        await s.flush()
        future = Block(
            plan_version_id=v.id,
            starts_at=clk.now() + timedelta(days=3),
            ends_at=clk.now() + timedelta(days=3, minutes=50),
            exam_id=1,
            topic_id=1,
            kind="learn",
        )
        s.add(future)
        await checkin.ask(s, clk.today())
        await s.commit()
    reply = {"is_report": True, "done_block_ids": [future.id], "skipped_block_ids": [], "extra": []}
    llm = LlmClient(FakeBackend(text_msg(json.dumps(reply))), db, SETTINGS, clk)
    out = await checkin.handle_answer(db, llm, clk, clk.today(), "сделал всё")
    assert out.done == 0
    async with db() as s:
        assert (await s.get(Block, future.id)).status == "planned"


def test_report_prefilter() -> None:
    assert checkin.looks_like_report("сегодня сделал 2 блока по TCP")
    assert checkin.looks_like_report("ничего не успел")
    assert not checkin.looks_like_report("а когда экзамен по алгебре?")


async def test_double_tap_rating_does_not_reset_level(db) -> None:
    async with db() as s:
        t = await s.scalar(select(Topic).where(Topic.name == "TCP"))
        t.status, t.level, t.progress_min = "active", 2, 90
        await s.commit()
        with pytest.raises(ValueError):
            await topics_service.rate_topic(s, t.id, 0)
        assert (await s.get(Topic, t.id)).progress_min == 90


async def test_draft_topics_are_editable_by_number(db) -> None:
    async with db() as s:
        for t in (await s.scalars(select(Topic).where(Topic.exam_id == 1))).all():
            t.status = "draft"
        await s.commit()
        second = await find_topic(s, "2", exam="Networks")
        assert second.name == "Routing"
    ctx = ToolContext(db, SETTINGS, FixedClock(at(date(2026, 11, 19), 10), HKT))
    _, err = await run_tool(
        ctx, "chat", "manage_topics", {"action": "remove", "exam": "Networks", "name": "1"}
    )
    assert not err


async def test_get_plan_shows_done_blocks(db) -> None:
    clk = FixedClock(at(date(2026, 11, 19), 7), HKT)
    svc = Services(SETTINGS, db, clk, FakeCanvasSource(), Recorder())
    await apply_choice(svc, await propose_replan(svc, "x", send=False), "A")
    async with db() as s:
        first = await s.scalar(select(Block).order_by(Block.starts_at))
        first.status = "done"
        await s.commit()
    text, _ = await run_tool(ToolContext(db, SETTINGS, clk), "chat", "get_plan", {})
    assert "[done]" in text and "[planned]" in text


async def test_morning_does_not_resend_a_shown_card(db) -> None:
    clk = FixedClock(at(date(2026, 11, 19), 7), HKT)
    svc = Services(SETTINGS, db, clk, FakeCanvasSource(), Recorder())
    await apply_choice(svc, await propose_replan(svc, "x", send=False), "A")
    async with db() as s:
        s.add(
            Proposal(
                created_at=clk.now(),
                kind="plan",
                summary_ru="shown",
                options_json=[{"key": "KEEP"}],
                status="pending",
                tg_message_id=7,
                expires_at=clk.now() + timedelta(hours=10),
            )
        )
        await s.commit()
    clk.advance(hours=1, minutes=30)
    await jobs.morning(svc)
    assert not any("shown" in t for t in svc.notifier.texts())


def test_safe_html_drops_unbalanced_tags() -> None:
    assert safe_html("<b>cut off") == "cut off"
    assert safe_html("<b><i>x</b></i>") == "x"
    assert safe_html("<b>ok</b> a<b") == "<b>ok</b> a&lt;b"
