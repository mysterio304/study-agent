"""Topic discovery (fixture, Batches, timeout fallback), review/rating flow, evening check-in."""

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent.brain.batches import BatchRunner
from agent.brain.llm import LlmClient
from agent.canvas.fake import FakeCanvasSource
from agent.canvas.structure import sync_course_structure
from agent.canvas.sync import sync_courses
from agent.config import Settings
from agent.core.clock import FixedClock
from agent.db.models import (
    Block,
    Checkin,
    CourseFile,
    Exam,
    LlmUsage,
    PlanVersion,
    StudySession,
    Topic,
)
from agent.prep import checkin as checkin_mod
from agent.prep import topics_service as ts
from agent.prep.discovery import TopicDiscovery, parse_topics
from agent.prep.mapping_service import confirm_mapping, suggestions_for_unconfirmed
from agent.prep.seed import seed_exams
from tests.brain.conftest import FakeBackend, text_msg
from tests.planner.conftest import HKT, at

SETTINGS = Settings(_env_file=None)


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(at(date(2026, 11, 19), 10, 0), HKT)


@pytest.fixture
async def mapped(sessionmaker, clk):
    async with sessionmaker() as s:
        await seed_exams(s)
        await sync_courses(s, FakeCanvasSource(), clk)
        for exam, sug in await suggestions_for_unconfirmed(s):
            await confirm_mapping(s, exam.id, sug.course_id)
        await s.commit()
    return sessionmaker


async def test_module_items_expose_files_when_files_tab_is_hidden(mapped) -> None:
    await sync_course_structure(mapped, FakeCanvasSource(), 101)
    async with mapped() as s:
        files = (await s.scalars(select(CourseFile).order_by(CourseFile.canvas_file_id))).all()
    assert [f.canvas_file_id for f in files] == [5501, 5502, 5503]
    assert {f.source for f in files} == {"module_item"}
    assert files[2].kind == "past_paper"


async def test_fixture_discovery_creates_drafts_and_fills_weights(mapped, clk) -> None:
    ready: list[int] = []

    async def on_ready(exam_id: int) -> None:
        ready.append(exam_id)

    d = TopicDiscovery(mapped, FakeCanvasSource(), None, None, clk, on_ready)
    assert len(await d.start()) == 4
    assert sorted(ready) == [1, 2, 3, 4]
    async with mapped() as s:
        drafts = (await s.scalars(select(Topic).where(Topic.status == "draft"))).all()
        algo = await s.scalar(select(Exam).where(Exam.title.like("Design%")))
        networks = await s.scalar(select(Exam).where(Exam.title == "Computer Networks"))
    assert len(drafts) > 20
    assert (algo.final_weight, algo.weight_source) == (0.6, "syllabus")
    assert networks.weight_source == "user"  # user-provided weight is never overwritten
    assert await d.start() == []  # idempotent: exams with topics are skipped


class FakeBatches:
    def __init__(self, statuses: list[str], payload: dict[str, str]) -> None:
        self.statuses, self.payload, self.created, self.cancelled = statuses, payload, [], []

    async def create(self, *, requests):
        self.created.append(requests)
        return SimpleNamespace(id="msgbatch_1")

    async def retrieve(self, batch_id):
        return SimpleNamespace(processing_status=self.statuses.pop(0))

    async def cancel(self, batch_id):
        self.cancelled.append(batch_id)

    async def results(self, batch_id):
        async def gen():
            for cid, text in self.payload.items():
                msg = SimpleNamespace(
                    model="claude-sonnet-5-5",
                    content=[SimpleNamespace(type="text", text=text)],
                    usage=SimpleNamespace(input_tokens=5000, output_tokens=1500),
                )
                yield SimpleNamespace(
                    custom_id=cid, result=SimpleNamespace(type="succeeded", message=msg)
                )

        return gen()


TOPICS_JSON = json.dumps(
    {
        "topics": [{"name": "TCP", "importance": 3, "effort_hours": 4, "sources": ["W4"]}],
        "final_weight": 0.65,
    }
)


async def test_discovery_via_batches_records_half_price_usage(mapped, clk) -> None:
    llm = LlmClient(FakeBackend(), mapped, SETTINGS, clk)
    fake = FakeBatches(["in_progress", "ended"], {f"exam-{i}": TOPICS_JSON for i in (1, 2, 3, 4)})
    runner = BatchRunner(fake, llm, mapped, SETTINGS, clk)
    ready: list[int] = []

    async def on_ready(eid: int) -> None:
        ready.append(eid)

    await TopicDiscovery(mapped, FakeCanvasSource(), llm, runner, clk, on_ready).start()
    params = fake.created[0][0]["params"]
    assert params["model"] == "claude-sonnet-5-5"
    assert params["output_config"]["format"]["type"] == "json_schema"
    await runner.poll()
    assert ready == []
    await runner.poll()
    assert sorted(ready) == [1, 2, 3, 4]
    async with mapped() as s:
        usage = (await s.scalars(select(LlmUsage))).all()
    assert len(usage) == 4 and all(u.batch for u in usage)
    assert usage[0].cost_usd == pytest.approx((5000 * 2 + 1500 * 10) / 1e6 / 2)


async def test_overdue_batch_is_cancelled_and_run_on_demand(mapped, clk) -> None:
    backend = FakeBackend(*[text_msg(TOPICS_JSON) for _ in range(4)])
    llm = LlmClient(backend, mapped, SETTINGS, clk)
    fake = FakeBatches(["in_progress"], {})
    runner = BatchRunner(fake, llm, mapped, SETTINGS, clk)

    async def on_ready(eid: int) -> None:
        return None

    await TopicDiscovery(mapped, FakeCanvasSource(), llm, runner, clk, on_ready).start()
    clk.advance(minutes=25)
    await runner.poll()
    assert fake.cancelled == ["msgbatch_1"] and len(backend.calls) == 4


def test_parse_topics_clamps_values() -> None:
    raw = json.dumps(
        {
            "topics": [
                {"name": " X ", "importance": 9, "effort_hours": 99, "sources": []},
                {"name": "", "importance": 1, "effort_hours": 1, "sources": []},
            ],
            "final_weight": 1.7,
        }
    )
    topics, weight = parse_topics(raw)
    assert topics == [{"name": "X", "importance": 3, "effort_hours": 12.0, "sources": []}]
    assert weight is None


async def test_review_and_rating_flow(mapped, clk) -> None:
    async def noop(eid: int) -> None:
        return None

    await TopicDiscovery(mapped, FakeCanvasSource(), None, None, clk, noop).start()
    async with mapped() as s:
        assert not await ts.setup_complete(s)
        for exam_id in (1, 2, 3, 4):
            await ts.accept_topics(s, exam_id)
        first = await ts.next_to_rate(s)
        assert first.exam_id == 1
        await ts.rate_topic(s, first.id, 2)
        assert (await s.get(Topic, first.id)).status == "active"
        for exam_id in (1, 2, 3, 4):
            await ts.rate_rest_of_exam(s, exam_id, 1)
        assert await ts.next_to_rate(s) is None
        assert await ts.setup_complete(s)


async def test_checkin_parses_report_into_blocks_and_sessions(mapped, clk) -> None:
    async with mapped() as s:
        t = Topic(
            exam_id=1,
            name="TCP",
            effort_hours=3,
            importance=3,
            level=1,
            target_level=2,
            status="active",
        )
        s.add(t)
        v = PlanVersion(
            created_at=clk.now(),
            reason="t",
            proposed_by="planner",
            applied_at=clk.now(),
            snapshot_json={"blocks": []},
        )
        s.add(v)
        await s.flush()
        b1 = Block(
            plan_version_id=v.id,
            starts_at=clk.now() + timedelta(hours=1),
            ends_at=clk.now() + timedelta(hours=1, minutes=50),
            exam_id=1,
            topic_id=t.id,
            kind="learn",
        )
        s.add(b1)
        await checkin_mod.ask(s, clk.today())
        await s.commit()
        reply = {
            "is_report": True,
            "done_block_ids": [b1.id],
            "skipped_block_ids": [],
            "extra": [{"topic_id": t.id, "minutes": 40, "kind": "practice"}],
        }
        assert await checkin_mod.awaiting(s, clk) == clk.today()
    llm = LlmClient(FakeBackend(text_msg(json.dumps(reply))), mapped, SETTINGS, clk)
    out = await checkin_mod.handle_answer(
        mapped, llm, clk, clk.today(), "сделал TCP и ещё 40 минут задач"
    )
    assert out.is_report and out.done == 1 and out.extra_minutes == 40
    async with mapped() as s:
        assert (await s.get(Checkin, clk.today())).status == "done"
        assert len((await s.scalars(select(StudySession))).all()) == 2
        assert await checkin_mod.awaiting(s, clk) is None


async def test_checkin_non_report_is_passed_through(mapped, clk) -> None:
    async with mapped() as s:
        await checkin_mod.ask(s, clk.today())
        await s.commit()
    llm = LlmClient(
        FakeBackend(
            text_msg(
                json.dumps(
                    {"is_report": False, "done_block_ids": [], "skipped_block_ids": [], "extra": []}
                )
            )
        ),
        mapped,
        SETTINGS,
        clk,
    )
    out = await checkin_mod.handle_answer(
        mapped, llm, clk, clk.today(), "а когда экзамен по сетям?"
    )
    assert not out.is_report
    async with mapped() as s:
        assert (await s.get(Checkin, clk.today())).status == "asked"
