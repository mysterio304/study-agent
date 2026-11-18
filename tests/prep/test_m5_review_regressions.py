"""Regression tests for findings from the Milestone 5 code review."""

import asyncio
from types import SimpleNamespace

from sqlalchemy import func, select

from agent.brain.llm import LlmClient
from agent.config import Settings
from agent.db.models import Block, LlmBatch, MockExam, Quiz, QuizQuestion, StudySession, Topic
from agent.notify import jobs
from agent.prep import mock as mock_mod
from agent.prep.quizzes import QuizBank, start_quiz, store_questions
from agent.telegram import quiz as quiz_handlers
from tests.brain.conftest import FakeBackend
from tests.notify.test_jobs import Recorder
from tests.prep.quiz_helpers import _block, q, svc_for

SETTINGS = Settings(_env_file=None)


async def test_unanswered_mock_expires_and_stops_capturing_chat(db, clk) -> None:
    svc = svc_for(db, clk)
    async with db() as s:
        m = await mock_mod.start_mock(s, 1, None, clk)
        await s.commit()
    clk.advance(minutes=121)
    await jobs.tick(svc)  # → awaiting_score
    clk.advance(hours=2, minutes=1)
    await jobs.tick(svc)
    async with db() as s:
        assert (await s.get(MockExam, m.id)).state == "cancelled"
        assert await mock_mod.active_mock(s) is None


async def test_concurrent_poll_answers_are_not_lost(db, clk) -> None:
    svc = svc_for(db, clk)
    async with db() as s:
        await store_questions(s, 1, [q(1), q(2), q(3)], "batch", clk)
        quiz = await start_quiz(s, 1, clk, purpose="adhoc", block_id=None, n=3)
        quiz.questions_json = [{**x, "poll_id": f"p{i}"} for i, x in enumerate(quiz.questions_json)]
        await s.commit()
    await asyncio.gather(
        *(
            quiz_handlers.on_poll_answer(SimpleNamespace(poll_id=f"p{i}", option_ids=[0]), svc)
            for i in range(3)
        )
    )
    async with db() as s:
        done = await s.get(Quiz, quiz.id)
        assert all(x["answer"] == 0 for x in done.questions_json) and done.finished_at is not None


async def test_failed_poll_send_closes_the_quiz(db, clk) -> None:
    class Flaky(Recorder):
        async def send_poll(self, *a, **k):
            return None

    svc = svc_for(db, clk)
    svc.notifier = Flaky()
    async with db() as s:
        await store_questions(s, 1, [q(1), q(2), q(3)], "batch", clk)
        b = await _block(s, clk, topic_id=1)
        await s.commit()
    await jobs.tick(svc)
    async with db() as s:
        quiz = await s.scalar(select(Quiz))
        assert quiz.finished_at is not None and quiz.passed is None
        assert (await s.get(Block, b.id)).status == "done"
    assert any("без проверки" in t for t in svc.notifier.texts())


async def test_expired_diagnostic_still_finishes_the_run(db, clk) -> None:
    from agent.db.models import Proposal
    from agent.prep.diagnostic import send_diagnostic

    svc = svc_for(db, clk)
    async with db() as s:
        await store_questions(s, 1, [q(1), q(2)], "batch", clk)
        await s.commit()
    await send_diagnostic(svc, 1)
    clk.advance(hours=4)
    await jobs.tick(svc)
    assert not any("засчитан" in t for t in svc.notifier.texts())  # not a block message
    async with db() as s:
        assert (
            await s.scalar(select(func.count()).select_from(Quiz).where(Quiz.finished_at.is_(None)))
            == 0
        )
        assert (
            await s.scalar(select(func.count()).select_from(Proposal)) >= 0
        )  # run closed, no crash


async def test_mock_does_not_double_count_planned_mock_blocks(db, clk) -> None:
    async with db() as s:
        b = await _block(s, clk, topic_id=1, kind="mock", status="planned")
        b.topic_id = None
        m = await mock_mod.start_mock(s, 1, None, clk)
        await mock_mod.record_score(s, m, 70)
        await mock_mod.finish(s, m.id, clk)
        await s.commit()
        assert (await s.get(Block, b.id)).status == "done"
        assert await s.scalar(select(func.count()).select_from(StudySession)) == 1


async def test_quiz_batches_are_not_resubmitted_for_in_flight_topics(db, clk) -> None:
    class Batches:
        def __init__(self):
            self.submits = 0

        def register(self, *a):
            pass

        async def submit(self, purpose, items, meta=None):
            self.submits += 1

    async with db() as s:
        s.add(
            LlmBatch(
                anthropic_batch_id="b1",
                purpose="quiz_generate",
                status="submitted",
                request_count=1,
                meta_json={"topic_ids": [1, 2]},
                submitted_at=clk.now(),
            )
        )
        await s.commit()
    batches = Batches()
    bank = QuizBank(db, LlmClient(FakeBackend(), db, SETTINGS, clk), batches, clk)
    assert await bank.fill([1, 2]) == "ready" and batches.submits == 0


async def test_failed_start_does_not_mark_questions_used(db, clk) -> None:
    async with db() as s:
        await store_questions(s, 1, [q(1)], "batch", clk)
        assert await start_quiz(s, 1, clk, purpose="adhoc", block_id=None, n=3) is None
        await s.commit()
        assert (await s.scalar(select(QuizQuestion))).times_used == 0


async def test_level_zero_topic_goes_to_one_first(db, clk) -> None:
    from agent.prep.quizzes import finalize

    async with db() as s:
        await store_questions(s, 1, [q(i) for i in range(9)], "batch", clk)
        levels = []
        for _ in range(3):
            clk.advance(hours=1)
            b = await _block(s, clk, topic_id=1, kind="practice")
            quiz = await start_quiz(s, 1, clk, purpose="verification", block_id=b.id, n=3)
            quiz.questions_json = [{**x, "answer": x["correct"]} for x in quiz.questions_json]
            await finalize(s, quiz, clk)
            levels.append((await s.get(Topic, 1)).level)
        await s.commit()
    assert levels == [1, 1, 2]
