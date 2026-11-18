"""Agent loop behaviour with a fake backend: routing, tools, escalation, refusals, limits, budget."""

from sqlalchemy import func, select

from agent.brain.agent import OVER_BUDGET_REPLY, STEP_LIMIT_REPLY
from agent.db.models import ChatMessage, LlmUsage, Proposal
from tests.brain.conftest import text_msg, tool_msg


async def test_simple_chat_uses_haiku_and_logs_cost(make_agent, seeded) -> None:
    agent, backend = make_agent(text_msg("Привет! Сегодня 7 блоков."))
    result = await agent.handle("привет, как дела?")
    assert result.text.startswith("Привет")
    assert backend.calls[0]["model"] == "claude-haiku-5-5"
    async with seeded() as s:
        usage = (await s.scalars(select(LlmUsage))).all()
        assert len(usage) == 1 and usage[0].purpose == "chat_turn" and usage[0].cost_usd > 0
        assert await s.scalar(select(func.count(ChatMessage.id))) == 2


async def test_parallel_tool_results_come_back_in_one_message(make_agent) -> None:
    agent, backend = make_agent(
        tool_msg(("get_progress", {}), ("list_agreements", {})),
        text_msg("Networks отстаёт."),
    )
    result = await agent.handle("как мой прогресс?")
    assert result.text == "Networks отстаёт." and result.steps == 2
    last_user = backend.calls[1]["messages"][-1]
    assert last_user["role"] == "user"
    assert [c["type"] for c in last_user["content"]] == ["tool_result", "tool_result"]
    assert "Долг" in last_user["content"][0]["content"]


async def test_escalate_restarts_turn_on_sonnet(make_agent) -> None:
    agent, backend = make_agent(tool_msg(("escalate", {"reason": "proof"})), text_msg("Разбор…"))
    result = await agent.handle("привет")
    assert [c["model"] for c in backend.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert result.model == "claude-sonnet-5-5"
    # The restarted turn does not carry Haiku's partial tool exchange.
    assert backend.calls[1]["messages"][-1]["content"][1]["text"] == "привет"


async def test_haiku_refusal_is_retried_once_on_sonnet(make_agent) -> None:
    agent, backend = make_agent(text_msg("", stop="refusal"), text_msg("Ок."))
    result = await agent.handle("вопрос")
    assert result.text == "Ок." and backend.calls[1]["model"] == "claude-sonnet-5-5"


async def test_plan_intent_routes_to_sonnet_with_plan_tools(make_agent) -> None:
    agent, backend = make_agent(text_msg("Давай обсудим."))
    await agent.handle("хочу забить на сети до 3 декабря")
    call = backend.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert "propose_plan_change" in [t["name"] for t in call["tools"]]
    assert call["thinking"] == {"type": "adaptive"} and call["output_config"] == {"effort": "low"}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]


async def test_step_limit_stops_the_loop(make_agent, settings) -> None:
    responses = [tool_msg(("get_progress", {})) for _ in range(settings.max_agent_steps)]
    agent, backend = make_agent(*responses)
    result = await agent.handle("прогресс")
    assert result.text == STEP_LIMIT_REPLY and len(backend.calls) == settings.max_agent_steps


async def test_propose_plan_change_creates_pending_proposal(make_agent, seeded) -> None:
    agent, _ = make_agent(
        tool_msg(
            (
                "propose_plan_change",
                {
                    "reason": "Пауза Networks до 25 ноя",
                    "pause_exam": "Networks",
                    "pause_until": "2026-11-25",
                },
            )
        ),
        text_msg("Предложил вариант, подтверди кнопкой."),
    )
    result = await agent.handle("не трогай сети до 25 ноября")
    assert len(result.proposals) == 1
    async with seeded() as s:
        p = await s.get(Proposal, result.proposals[0])
        assert p.status == "pending" and p.kind == "plan"


async def test_over_budget_stops_llm_and_downgrades(make_agent, seeded, clk) -> None:
    async with seeded() as s:
        s.add(LlmUsage(at=clk.now(), model="claude-sonnet-5-5", purpose="x", cost_usd=2.1))
        await s.commit()
    agent, backend = make_agent(text_msg("ok"))
    await agent.handle("хочу перенести план")  # plan intent, but ≥100 % → Haiku only
    assert backend.calls[0]["model"] == "claude-haiku-5-5"
    async with seeded() as s:
        s.add(LlmUsage(at=clk.now(), model="claude-sonnet-5-5", purpose="x", cost_usd=1.0))
        await s.commit()
    agent, backend = make_agent()
    assert (await agent.handle("привет")).text == OVER_BUDGET_REPLY
    assert backend.calls == []


async def test_requests_use_cache_breakpoints(make_agent, settings) -> None:
    agent, backend = make_agent(text_msg("a"))
    await agent.handle("привет")
    call = backend.calls[0]
    assert call["cache_control"] == {"type": "ephemeral", "ttl": settings.cache_ttl}
    assert "cache_control" not in call["system"][0]
    assert "fallbacks" not in call  # Haiku: no server-side fallback
    assert call["system"][1]["cache_control"]["ttl"] == settings.cache_ttl
    # Dynamic context rides in the newest user turn, after the cached prefix.
    assert call["messages"][-1]["content"][0]["text"].startswith("<context>")


async def test_escalation_after_a_write_does_not_repeat_it(make_agent, seeded) -> None:
    from agent.db.models import StudySession

    agent, backend = make_agent(
        tool_msg(("log_study_session", {"topic": "TCP", "minutes": 30})),
        tool_msg(("escalate", {"reason": "hard"})),
        text_msg("Записал и разобрал."),
    )
    result = await agent.handle("сделал 30 минут TCP, и объясни мне ещё одну вещь")
    assert result.text == "Записал и разобрал."
    assert [c["model"] for c in backend.calls][-1] == "claude-sonnet-5-5"
    async with seeded() as s:
        assert len((await s.scalars(select(StudySession))).all()) == 1
