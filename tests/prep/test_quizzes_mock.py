"""Quiz engine, block verification, diagnostic, importance proposals, mock exam mode."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from agent.brain.llm import LlmClient
from agent.config import Settings
from agent.db.models import (
    Block,
    CourseFile,
    LlmUsage,
    MockExam,
    PlanVersion,
    Proposal,
    Quiz,
    QuizQuestion,
    StudySession,
    Topic,
)
from agent.files.indexer import index_local_file
from agent.flows import apply_choice, propose_importance
from agent.notify import jobs
from agent.prep import mock as mock_mod
from agent.prep.diagnostic import maybe_finish, send_diagnostic
from agent.prep.quizzes import (
    QuizBank,
    expire_stale,
    finalize,
    generate_on_demand,
    parse_questions,
    record_answer,
    start_quiz,
    store_questions,
)
from tests.brain.conftest import FakeBackend, text_msg
from tests.files.samples import make_pdf
from tests.prep.quiz_helpers import QUIZ_JSON, _block, q, svc_for

SETTINGS = Settings(_env_file=None)

# --- generation & bank -----------------------------------------------------------------------


def test_parse_questions_enforces_telegram_limits_and_drops_bad_ones() -> None:
    raw = json.dumps(
        {
            "questions": [
                {
                    "question": "x" * 500,
                    "options": ["a" * 150, "b", "c", "d"],
                    "correct": 0,
                    "explanation": "e" * 400,
                    "source": "s",
                },
                {
                    "question": "bad index",
                    "options": ["a", "b"],
                    "correct": 5,
                    "explanation": "",
                    "source": "",
                },
                {
                    "question": "dupes",
                    "options": ["a", "a", "b"],
                    "correct": 0,
                    "explanation": "",
                    "source": "",
                },
            ]
        }
    )
    [ok] = parse_questions(raw)
    assert (
        len(ok["question"]) <= 300
        and len(ok["options"][0]) <= 100
        and len(ok["explanation"]) <= 200
    )


async def test_on_demand_generation_uses_course_excerpts(db, clk) -> None:
    backend = FakeBackend(text_msg(QUIZ_JSON))
    llm = LlmClient(backend, db, SETTINGS, clk)
    assert await generate_on_demand(db, llm, 1, clk) == 4
    prompt = backend.calls[0]["messages"][0]["content"]
    assert "L5.pdf" in prompt and "cwnd" in prompt
    assert backend.calls[0]["model"] == "claude-haiku-5-5"
    assert backend.calls[0]["output_config"]["format"]["type"] == "json_schema"


async def test_bank_reuses_least_used_questions(db, clk) -> None:
    async with db() as s:
        await store_questions(s, 1, [q(1), q(2), q(3), q(4)], "batch", clk)
        first = await start_quiz(s, 1, clk, purpose="adhoc", block_id=None, n=3)
        second = await start_quiz(s, 1, clk, purpose="adhoc", block_id=None, n=3)
        await s.commit()
    first_ids = {x["qid"] for x in first.questions_json}
    second_ids = {x["qid"] for x in second.questions_json}
    assert len(first_ids) == 3 and len(first_ids & second_ids) == 2  # the unused one comes first


async def test_nightly_batch_covers_tomorrows_topics(db, clk) -> None:
    class Batches:
        def __init__(self):
            self.items = None

        def register(self, *a):
            pass

        async def submit(self, purpose, items, meta=None):
            self.items = items

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
        s.add(
            Block(
                plan_version_id=v.id,
                starts_at=clk.now() + timedelta(hours=20),
                exam_id=1,
                topic_id=2,
                ends_at=clk.now() + timedelta(hours=21),
                kind="learn",
                status="planned",
            )
        )
        await s.commit()
    batches = Batches()
    bank = QuizBank(db, LlmClient(FakeBackend(), db, SETTINGS, clk), batches, clk)
    assert await bank.nightly() == "batched"
    assert [i.custom_id for i in batches.items] == ["topic-2"]


# --- verification ----------------------------------------------------------------------------


async def test_block_end_sends_polls_and_passing_levels_up(db, clk) -> None:
    svc = svc_for(db, clk, text_msg(QUIZ_JSON))
    async with db() as s:
        b = await _block(s, clk, topic_id=1, kind="learn")
        await s.commit()
    await jobs.tick(svc)
    polls = [t for t in svc.notifier.texts() if t.startswith("[poll]")]
    assert len(polls) == 3
    async with db() as s:
        quiz = await s.scalar(select(Quiz))
        ids = [x["poll_id"] for x in quiz.questions_json]
        outcome = None
        for i, pid in enumerate(ids):
            outcome = await record_answer(s, pid, 0 if i < 2 else 1, clk)  # 2/3 correct
        await s.commit()
        topic = await s.get(Topic, 1)
        study = await s.scalar(select(StudySession).where(StudySession.block_id == b.id))
    assert outcome.passed and outcome.level_change == (0, 1)
    assert topic.level == 1 and study.verified
    async with db() as s:
        assert (await s.get(Block, b.id)).status == "done"


async def test_failed_quiz_gives_half_credit_and_review_tomorrow(db, clk) -> None:
    async with db() as s:
        await store_questions(s, 2, [q(1), q(2), q(3)], "batch", clk)
        b = await _block(s, clk, topic_id=2, kind="practice")
        quiz = await start_quiz(s, 2, clk, purpose="verification", block_id=b.id, n=3)
        quiz.questions_json = [{**x, "poll_id": f"p{i}"} for i, x in enumerate(quiz.questions_json)]
        await s.commit()
        for i in range(3):
            out = await record_answer(s, f"p{i}", 3, clk)  # all wrong
        await s.commit()
        topic = await s.get(Topic, 2)
    assert out.passed is False and topic.level == 1
    assert topic.progress_min == 25  # 50 min block, only half counts
    assert topic.next_review_at == clk.now() + timedelta(days=1)


async def test_two_passed_practice_quizzes_reach_level_two(db, clk) -> None:
    async with db() as s:
        await store_questions(s, 2, [q(i) for i in range(8)], "batch", clk)
        changes = []
        for _ in range(2):
            b = await _block(s, clk, topic_id=2, kind="practice")
            quiz = await start_quiz(s, 2, clk, purpose="verification", block_id=b.id, n=3)
            for x in quiz.questions_json:
                x["answer"] = x["correct"]
            quiz.questions_json = list(quiz.questions_json)
            changes.append((await finalize(s, quiz, clk)).level_change)
        await s.commit()
    assert changes == [None, (1, 2)]


async def test_unanswered_quiz_expires_as_done_unverified(db, clk) -> None:
    async with db() as s:
        await store_questions(s, 1, [q(1), q(2), q(3)], "batch", clk)
        b = await _block(s, clk, topic_id=1)
        await start_quiz(s, 1, clk, purpose="verification", block_id=b.id, n=3)
        await s.commit()
    clk.advance(hours=4)
    async with db() as s:
        [o] = await expire_stale(s, clk)
        await s.commit()
        assert o.passed is None and (await s.get(Block, b.id)).status == "done"


async def test_no_material_falls_back_to_buttons(db, clk) -> None:
    svc = svc_for(db, clk, text_msg(json.dumps({"questions": []})))
    async with db() as s:
        s.add(
            Topic(
                id=9,
                exam_id=1,
                name="Quantum basket weaving",
                effort_hours=1,
                importance=1,
                level=0,
                target_level=2,
                status="active",
            )
        )
        await s.flush()
        await _block(s, clk, topic_id=9)
        await s.commit()
    await jobs.tick(svc)
    assert any("Как прошло?" in t for t in svc.notifier.texts())


# --- diagnostic & importance -----------------------------------------------------------------


async def test_diagnostic_proposes_levels_after_all_answers(db, clk) -> None:
    svc = svc_for(db, clk)
    async with db() as s:
        await store_questions(s, 1, [q(1), q(2)], "batch", clk)
        await store_questions(s, 2, [q(3), q(4)], "batch", clk)
        await s.commit()
    assert await send_diagnostic(svc, 1) == ""
    async with db() as s:
        quizzes = (await s.scalars(select(Quiz).where(Quiz.purpose == "diagnostic"))).all()
        for quiz in quizzes:
            right = quiz.topic_id == 1  # topic 1 all correct, topic 2 all wrong
            for x in quiz.questions_json:
                await record_answer(s, x["poll_id"], x["correct"] if right else 3, clk)
        await s.commit()
    for quiz in quizzes:
        await maybe_finish(svc, quiz.id)
    async with db() as s:
        p = await s.scalar(select(Proposal).where(Proposal.kind == "topic_levels"))
        assert {c["topic_id"]: c["value"] for c in p.options_json[0]["changes"]} == {1: 2}
        assert (await s.get(Topic, 1)).level == 0  # nothing applied silently
    assert (await apply_choice(svc, p.id, "A")).startswith("Обновил 1")
    async with db() as s:
        assert (await s.get(Topic, 1)).level == 2


async def test_past_paper_frequency_drives_importance_proposal(db, clk, tmp_path) -> None:
    svc = svc_for(db, clk)
    paper = make_pdf(
        tmp_path / "pp.pdf",
        [
            "Q1 TCP congestion control cwnd",
            "Q2 TCP congestion window",
            "Q3 congestion control fairness",
        ],
    )
    async with db() as s:
        s.add(
            CourseFile(
                id=2,
                course_id=101,
                display_name="Final 2024.pdf",
                source="telegram_upload",
                kind="past_paper",
                index_state="pending",
            )
        )
        await s.commit()
    await index_local_file(db, 2, paper, clk)
    pid = await propose_importance(svc, 101)
    async with db() as s:
        changes = (await s.get(Proposal, pid)).options_json[0]["changes"]
    # One paper: frequent topic → ★★★; an unmentioned topic is NOT downgraded yet (too little evidence)
    assert {c["topic_id"]: c["value"] for c in changes} == {1: 3}


def test_importance_rules() -> None:
    from agent.prep.topic_proposals import suggested_importance

    assert suggested_importance(2, hits=5, top_cut=4, papers=1) == 3
    assert suggested_importance(1, hits=1, top_cut=4, papers=1) == 2
    assert suggested_importance(3, hits=1, top_cut=4, papers=1) == 3
    assert suggested_importance(3, hits=0, top_cut=4, papers=1) == 3
    assert suggested_importance(3, hits=0, top_cut=4, papers=2) == 2
    assert suggested_importance(1, hits=0, top_cut=4, papers=3) == 1


# --- mock exam -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "score"),
    [
        ("72", 72.0),
        ("72%", 72.0),
        ("36/50", 72.0),
        ("около 81,5", 81.5),
        ("150", None),
        ("60/50", None),
        ("не знаю", None),
    ],
)
def test_parse_score(text: str, score: float | None) -> None:
    assert mock_mod.parse_score(text) == score


def test_mock_trigger() -> None:
    assert mock_mod.is_trigger("Начинаю past paper по сетям")
    assert not mock_mod.is_trigger("что такое past paper?")


async def test_mock_flow_pings_score_topics_and_plan_effects(db, clk) -> None:
    svc = svc_for(db, clk)
    async with db() as s:
        m = await mock_mod.start_mock(s, 1, None, clk)
        await s.commit()
    clk.advance(minutes=61)
    await jobs.tick(svc)
    clk.advance(minutes=50)
    await jobs.tick(svc)
    clk.advance(minutes=10)
    await jobs.tick(svc)
    await jobs.tick(svc)  # nothing more
    texts = svc.notifier.texts()
    assert sum("Половина" in t for t in texts) == 1 and sum("10 минут" in t for t in texts) == 1
    assert sum("Сколько баллов" in t for t in texts) == 1
    async with db() as s:
        m = await s.get(MockExam, m.id)
        assert m.state == "awaiting_score"
        await mock_mod.record_score(s, m, 64.0)
        await mock_mod.toggle_topic(s, m.id, 2)
        await mock_mod.toggle_topic(s, m.id, 1)
        await mock_mod.toggle_topic(s, m.id, 1)  # untoggle
        result = await mock_mod.finish(s, m.id, clk)
        await s.commit()
        topic2 = await s.get(Topic, 2)
        session = await s.scalar(select(StudySession).where(StudySession.kind == "mock"))
    assert result[1] == ["Routing algorithms"] and topic2.level == 0 and session.minutes == 120


async def test_search_tool_cites_sources(db, clk) -> None:
    from agent.brain.tool_handlers import ToolContext
    from agent.brain.tools import run_tool

    text, err = await run_tool(
        ToolContext(db, SETTINGS, clk),
        "material",
        "search_course_files",
        {"query": "slow start cwnd", "course": "Networks"},
    )
    assert not err and text.startswith("[1] L5.pdf, ") and "cwnd" in text
    async with db() as s:
        assert await s.scalar(select(func.count()).select_from(LlmUsage)) == 0  # search is free
        assert await s.scalar(select(func.count()).select_from(QuizQuestion)) == 0
