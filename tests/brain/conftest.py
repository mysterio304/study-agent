"""Fake Anthropic backend and a seeded DB for brain tests."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from agent.brain.agent import Agent
from agent.brain.llm import LlmClient
from agent.config import Settings
from agent.core.clock import FixedClock
from agent.db.models import Topic
from agent.prep.seed import seed_exams
from tests.planner.conftest import HKT, at


def text_msg(
    text: str, *, stop: str = "end_turn", inp: int = 900, out: int = 120, read: int = 0
) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop,
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=read,
            cache_creation_input_tokens=0,
            cache_creation=None,
        ),
    )


def tool_msg(*calls: tuple[str, dict[str, Any]]) -> Any:
    blocks = [
        SimpleNamespace(type="tool_use", id=f"toolu_{i}", name=n, input=a)
        for i, (n, a) in enumerate(calls)
    ]
    return SimpleNamespace(
        content=blocks,
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=800,
            output_tokens=60,
            cache_read_input_tokens=3000,
            cache_creation_input_tokens=200,
            cache_creation=None,
        ),
    )


class FakeBackend:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **params: Any) -> Any:
        self.calls.append(params)
        if not self.responses:
            raise AssertionError("FakeBackend ran out of responses")
        return self.responses.pop(0)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, daily_llm_budget_usd=2.0)


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(at(date(2026, 11, 19), 14, 0), HKT)


@pytest.fixture
async def seeded(sessionmaker):
    async with sessionmaker() as s:
        await seed_exams(s)
        await s.flush()
        for exam_id, names in {
            1: ["TCP", "Routing"],
            2: ["Dynamic programming"],
            3: ["Eigenvalues"],
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


@pytest.fixture
def make_agent(seeded, settings, clk):
    def _make(*responses: Any) -> tuple[Agent, FakeBackend]:
        backend = FakeBackend(*responses)
        llm = LlmClient(backend, seeded, settings, clk)
        return Agent(llm, seeded, settings, clk), backend

    return _make
