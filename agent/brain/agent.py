"""The conversational agent: one user turn → routed, budgeted, cached tool-use loop.

Flow (ARCHITECTURE §4, §11):
  route (rule-based) → build prompt (stable prefix + history + dynamic context) → loop up to
  MAX_AGENT_STEPS: call → run tool calls in parallel → … → final text.
  `escalate` or a Haiku refusal restarts the turn once on the smart model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.brain.budgets import PromptTooLarge
from agent.brain.context import dynamic_context, static_profile, system_blocks
from agent.brain.llm import BudgetExceeded, LlmClient, block_to_param, text_of
from agent.brain.memory import (
    history_messages,
    pending_for_summary,
    save_message,
    store_summary,
    summary_prompt,
)
from agent.brain.router import Route, route
from agent.brain.tool_handlers import ToolContext
from agent.brain.tools import run_tools_parallel, tool_definitions
from agent.config import Settings
from agent.core.clock import Clock
from agent.db.models import ConversationSummary, Proposal

log = logging.getLogger(__name__)

OVER_BUDGET_REPLY = (
    "Лимит на LLM на сегодня исчерпан — до полуночи отвечаю только шаблонами: /today, /plan, "
    "/progress, /usage работают. Напоминания и пинги тоже работают."
)
STEP_LIMIT_REPLY = "Я упёрся в лимит шагов на один ответ. Сформулируй вопрос чуть уже, пожалуйста."


@dataclass
class TurnResult:
    text: str
    proposals: list[int] = field(default_factory=list)
    route: Route | None = None
    model: str | None = None
    steps: int = 0
    cost_usd: float = 0.0


class Agent:
    def __init__(
        self, llm: LlmClient, sessionmaker: async_sessionmaker, settings: Settings, clock: Clock
    ) -> None:
        self._llm, self._sm, self._settings, self._clock = llm, sessionmaker, settings, clock

    async def handle(self, text: str, *, via: str = "text") -> TurnResult:
        budget = await self._llm.budget()
        if budget.mode == "stopped":
            return TurnResult(OVER_BUDGET_REPLY)
        async with self._sm() as s:
            pending = (
                await s.scalar(select(Proposal.id).where(Proposal.status == "pending")) is not None
            )
            profile_text = await static_profile(s, self._settings)
            history = await history_messages(s, self._clock)
            context = await dynamic_context(s, self._settings, self._clock)
            await save_message(s, "user", text, self._clock, via=via)
            await s.commit()

        r = route(text, pending_proposal=pending, budget=budget.mode)
        user_turn = {
            "role": "user",
            "content": [{"type": "text", "text": context}, {"type": "text", "text": text}],
        }
        system = system_blocks(profile_text, self._settings.cache_ttl)
        ctx = ToolContext(self._sm, self._settings, self._clock)
        try:
            result = await self._loop(r, system, history, user_turn, ctx)
        except BudgetExceeded:
            result = TurnResult(OVER_BUDGET_REPLY)
        except PromptTooLarge:
            log.warning("Prompt over budget; retrying without history")
            try:
                result = await self._loop(r, system, [], user_turn, ctx)
            except (PromptTooLarge, BudgetExceeded):
                result = TurnResult(
                    "Сообщение слишком длинное для одного ответа — сократи, пожалуйста."
                )
        result.proposals = ctx.proposals
        async with self._sm() as s:
            await save_message(s, "assistant", result.text, self._clock)
            await s.commit()
        await self._maybe_summarise()
        return result

    async def _loop(
        self,
        r: Route,
        system: list[dict[str, Any]],
        history: list[dict[str, Any]],
        user_turn: dict[str, Any],
        ctx: ToolContext,
    ) -> TurnResult:
        role, escalated, steps, cost = r.role, r.role == "smart", 0, 0.0
        messages = [*history, user_turn]
        base = len(messages)  # messages beyond this index are this turn's tool exchanges
        tools = tool_definitions(r.profile)
        while steps < self._settings.max_agent_steps:
            call_type = "escalated_turn" if role == "smart" else "chat_turn"
            res = await self._llm.call(
                call_type,
                system=system,
                messages=messages,
                tools=tools,
                role=role,
                route_reason=r.reason,
                prefix_key=f"{role}:{r.profile}",
            )
            steps, cost = steps + 1, cost + res.cost_usd
            msg = res.message
            if msg.stop_reason == "refusal" and not escalated:
                # Retry on the smart model. Tool results already in `messages` are kept, so tools
                # that wrote something are never run twice.
                log.info("Haiku refused; retrying once on the smart model")
                role, escalated = "smart", True
                continue
            if msg.stop_reason == "pause_turn":
                messages.append(
                    {"role": "assistant", "content": [block_to_param(b) for b in msg.content]}
                )
                continue
            calls = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
            if not calls:
                text = text_of(msg) or "Готово."
                return TurnResult(text, route=r, model=res.model, steps=steps, cost_usd=cost)
            if any(c.name == "escalate" for c in calls) and not escalated:
                role, escalated = "smart", True
                if len(messages) == base and len(calls) == 1:
                    # Nothing ran yet: restart cleanly on the smart model (cheaper prefix).
                    ctx.escalate_reason = None
                    continue
                # Otherwise fall through: run this step's tools (escalate just returns a note)
                # and continue the same exchange on the smart model — no write runs twice.
            messages.append(
                {"role": "assistant", "content": [block_to_param(b) for b in msg.content]}
            )
            messages.append(
                {"role": "user", "content": await run_tools_parallel(ctx, r.profile, calls)}
            )
        return TurnResult(STEP_LIMIT_REPLY, route=r, steps=steps, cost_usd=cost)

    async def _maybe_summarise(self) -> None:
        """Refresh the rolling summary with Haiku once enough messages left the live window."""
        async with self._sm() as s:
            older = await pending_for_summary(s, self._clock)
            if not older:
                return
            row = await s.get(ConversationSummary, 1)
            prompt = summary_prompt(row.text if row else "", older)
        try:
            res = await self._llm.call(
                "history_summary",
                system="You compress chat history.",
                messages=[{"role": "user", "content": prompt}],
            )
        except (BudgetExceeded, PromptTooLarge) as exc:
            log.info("Skipping history summary: %s", exc)
            return
        async with self._sm() as s:
            await store_summary(s, text_of(res.message), older[-1].id, self._clock)
            await s.commit()
