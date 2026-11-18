"""Prompt-size budgets per call type, byte stability, routing rules, cost maths (SPEC §9a)."""

import re
from datetime import timedelta

import pytest

from agent.brain.accounting import UsageNumbers, budget_mode, cost_usd, usage_from_response
from agent.brain.budgets import (
    CALL_SPECS,
    DYNAMIC_CONTEXT_BUDGET,
    HISTORY_BUDGET,
    STATIC_PREFIX_BUDGET,
    TOOL_RESULT_BUDGET,
    PromptTooLarge,
    check_request,
    estimate_tokens,
    truncate_to_tokens,
)
from agent.brain.context import SYSTEM_PROMPT, dynamic_context, static_profile, system_blocks
from agent.brain.llm import thinking_params
from agent.brain.memory import history_messages, save_message
from agent.brain.router import route
from agent.brain.tools import PROFILES, profile_fingerprint, tool_definitions
from agent.db.models import Block, PlanVersion

# --- prompt-size budgets ----------------------------------------------------------------------


async def test_static_prefix_fits_budget_for_every_profile(seeded, settings) -> None:
    async with seeded() as s:
        profile = await static_profile(s, settings)
    system = system_blocks(profile, "5m")
    for name in PROFILES:
        est = estimate_tokens(system) + estimate_tokens(tool_definitions(name))
        assert est <= STATIC_PREFIX_BUDGET, f"{name}: {est}"
        assert est >= 512, "prefix below the 512-token minimum would not be cached"


async def test_dynamic_context_fits_budget_on_a_busy_day(seeded, settings, clk) -> None:
    async with seeded() as s:
        v = PlanVersion(
            created_at=clk.now(),
            reason="t",
            proposed_by="planner",
            applied_at=clk.now(),
            snapshot_json={"blocks": []},
        )
        s.add(v)
        await s.flush()
        for i in range(12):
            start = clk.now() + timedelta(hours=i)
            s.add(
                Block(
                    plan_version_id=v.id,
                    starts_at=start,
                    ends_at=start + timedelta(minutes=50),
                    exam_id=1,
                    topic_id=1,
                    kind="learn",
                    status="planned",
                )
            )
        await s.commit()
        ctx = await dynamic_context(s, settings, clk)
    assert estimate_tokens(ctx) <= DYNAMIC_CONTEXT_BUDGET


async def test_history_is_windowed_and_within_budget(seeded, clk) -> None:
    async with seeded() as s:
        for i in range(30):
            await save_message(
                s, "user" if i % 2 == 0 else "assistant", "очень длинное сообщение " * 120, clk
            )
        await s.commit()
        msgs = await history_messages(s, clk)
    assert estimate_tokens(msgs) <= HISTORY_BUDGET
    assert len(msgs) <= 8 and msgs[0]["role"] == "user"


def test_tool_results_and_rag_chunks_are_truncated() -> None:
    long = "x" * 50_000
    assert estimate_tokens(truncate_to_tokens(long, TOOL_RESULT_BUDGET)) <= TOOL_RESULT_BUDGET
    assert estimate_tokens(truncate_to_tokens("слово " * 2000, 400)) <= 400


@pytest.mark.parametrize("name", sorted(CALL_SPECS))
def test_every_call_type_has_output_and_input_limits(name: str) -> None:
    spec = CALL_SPECS[name]
    assert 0 < spec.max_tokens <= 4_000
    assert 0 < spec.input_budget <= 20_000


def test_check_request_enforces_budget_and_haiku_ceiling() -> None:
    spec = CALL_SPECS["checkin_parse"]
    with pytest.raises(PromptTooLarge):
        check_request(
            spec, "claude-haiku-5-5", system="s", tools=None, messages=[{"content": "я" * 20_000}]
        )
    big = CALL_SPECS["escalated_turn"]
    with pytest.raises(PromptTooLarge, match="Haiku"):
        check_request(
            big, "claude-haiku-5-5", system="s", tools=None, messages=[{"content": "a" * 300_000}]
        )


# --- byte stability ---------------------------------------------------------------------------


def test_tools_are_sorted_and_byte_stable() -> None:
    for name in PROFILES:
        defs = tool_definitions(name)
        assert [d["name"] for d in defs] == sorted(d["name"] for d in defs)
        assert profile_fingerprint(name) == profile_fingerprint(name)


def test_system_prompt_has_no_volatile_content() -> None:
    assert not re.search(r"20\d\d-\d\d-\d\d|\d{1,2}:\d\d", SYSTEM_PROMPT)


async def test_static_profile_is_stable_across_time(seeded, settings, clk) -> None:
    async with seeded() as s:
        a = await static_profile(s, settings)
        clk.advance(hours=5)
        b = await static_profile(s, settings)
    assert a == b


# --- routing ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "role", "profile"),
    [
        ("привет", "cheap", "chat"),
        ("перенеси сегодняшний блок на вечер", "smart", "plan"),
        ("забью на сети до 3 декабря", "smart", "plan"),
        ("что такое TCP congestion control?", "cheap", "material"),
        ("объясни подробно и по шагам доказательство корректности Dijkstra", "smart", "material"),
        ("сколько нужно на final для A?", "cheap", "grades"),
    ],
)
def test_router(text: str, role: str, profile: str) -> None:
    r = route(text, pending_proposal=False, budget="normal")
    assert (r.role, r.profile) == (role, profile)


def test_router_downgrades_when_over_budget_and_follows_open_proposal() -> None:
    assert route("перенеси план", pending_proposal=False, budget="haiku_only").role == "cheap"
    assert route("а если вариант Б?", pending_proposal=True, budget="normal").profile == "plan"


# --- accounting -------------------------------------------------------------------------------


def test_cost_maths_and_batch_discount() -> None:
    u = UsageNumbers(input_tokens=1_000_000, output_tokens=1_000_000, cache_read=1_000_000)
    assert cost_usd("claude-haiku-5-5", u) == pytest.approx(0.10 + 0.50 + 0.01)
    assert cost_usd("claude-sonnet-5-5", u, batch=True) == pytest.approx((2 + 10 + 0.1) / 2)
    assert cost_usd("unknown-model", UsageNumbers(output_tokens=1_000_000)) == pytest.approx(10.0)


def test_usage_split_by_ttl() -> None:
    from types import SimpleNamespace

    u = usage_from_response(
        SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=50,
            cache_creation=SimpleNamespace(
                ephemeral_5m_input_tokens=20, ephemeral_1h_input_tokens=30
            ),
        )
    )
    assert (u.cache_write_5m, u.cache_write_1h) == (20, 30)


@pytest.mark.parametrize(
    ("spent", "mode"), [(0.5, "normal"), (1.7, "warn"), (2.0, "haiku_only"), (3.0, "stopped")]
)
def test_budget_modes(spent: float, mode: str) -> None:
    assert budget_mode(spent, 2.0, 80) == mode


def test_thinking_params_per_model() -> None:
    assert thinking_params("claude-haiku-5-5", "off")["thinking"] == {"type": "disabled"}
    assert thinking_params("claude-sonnet-5-5", "off")["thinking"] == {"type": "between_tools"}
    assert thinking_params("claude-sonnet-5-5", "medium")["output_config"] == {"effort": "medium"}
