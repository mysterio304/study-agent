"""Shared helpers for quiz/mock tests (fixtures live in tests/prep/conftest.py)."""

from __future__ import annotations

import json
from datetime import timedelta

from agent.brain.llm import LlmClient
from agent.canvas.fake import FakeCanvasSource
from agent.config import Settings
from agent.db.models import Block, PlanVersion
from agent.flows import Services
from agent.prep.quizzes import QuizBank
from tests.brain.conftest import FakeBackend
from tests.notify.test_jobs import Recorder

SETTINGS = Settings(_env_file=None)


def q(i: int, correct: int = 0) -> dict:
    return {
        "question": f"Q{i}: What does cwnd control?",
        "options": ["send rate", "MTU", "TTL", "port"],
        "correct": correct,
        "explanation": "cwnd limits unacked data",
        "source": "L5.pdf, p. 3",
    }


QUIZ_JSON = json.dumps({"questions": [q(1), q(2), q(3), q(4)]})


def svc_for(db, clk, *responses) -> Services:
    svc = Services(SETTINGS, db, clk, FakeCanvasSource(), Recorder())
    svc.llm = LlmClient(FakeBackend(*responses), db, SETTINGS, clk)
    svc.quizzes = QuizBank(db, svc.llm, None, clk)
    return svc


async def _block(s, clk, topic_id: int, kind: str = "learn", status: str = "started") -> Block:
    v = PlanVersion(
        created_at=clk.now(),
        reason="t",
        proposed_by="planner",
        applied_at=clk.now(),
        snapshot_json={"blocks": []},
    )
    s.add(v)
    await s.flush()
    b = Block(
        plan_version_id=v.id,
        starts_at=clk.now() - timedelta(minutes=50),
        ends_at=clk.now(),
        exam_id=1,
        topic_id=topic_id,
        kind=kind,
        status=status,
    )
    s.add(b)
    await s.flush()
    return b
