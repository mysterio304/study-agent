"""Cost accounting and the daily budget guard (SPEC §9a.8)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.core.clock import Clock
from agent.core.timeutil import local_to_utc
from agent.db.models import LlmUsage

# USD per million tokens: input, output, cache read, 5-minute cache write, 1-hour cache write.
# Haiku 5.5 has a second rate card above 100K-token prompts; we never send those (PromptGuard).
PRICES: Mapping[str, tuple[float, float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50, 4.00),
}
FALLBACK_PRICE = PRICES["claude-sonnet-5-5"]  # unknown model → assume the expensive one
BATCH_DISCOUNT = 0.5
STOP_FACTOR = 1.5

BudgetMode = Literal["normal", "warn", "haiku_only", "stopped"]


@dataclass(frozen=True, slots=True)
class UsageNumbers:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0


def usage_from_response(usage: Any) -> UsageNumbers:
    """Extract token counts from an SDK `usage` object (or a test double)."""
    creation = getattr(usage, "cache_creation", None)
    w5 = getattr(creation, "ephemeral_5m_input_tokens", None) if creation else None
    w1 = getattr(creation, "ephemeral_1h_input_tokens", None) if creation else None
    total_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    if w5 is None and w1 is None:
        w5, w1 = total_write, 0
    return UsageNumbers(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_5m=w5 or 0,
        cache_write_1h=w1 or 0,
    )


def cost_usd(model: str, u: UsageNumbers, *, batch: bool = False) -> float:
    p_in, p_out, p_read, p_w5, p_w1 = PRICES.get(model, FALLBACK_PRICE)
    total = (
        u.input_tokens * p_in
        + u.output_tokens * p_out
        + u.cache_read * p_read
        + u.cache_write_5m * p_w5
        + u.cache_write_1h * p_w1
    ) / 1_000_000
    return total * (BATCH_DISCOUNT if batch else 1.0)


async def record_usage(
    session: AsyncSession,
    clock: Clock,
    *,
    model: str,
    purpose: str,
    usage: UsageNumbers,
    batch: bool = False,
    route_reason: str | None = None,
    prefix_key: str | None = None,
) -> LlmUsage:
    row = LlmUsage(
        at=clock.now(),
        model=model,
        purpose=purpose,
        route_reason=route_reason,
        prefix_key=prefix_key,
        batch=batch,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read,
        cache_write_5m_tokens=usage.cache_write_5m,
        cache_write_1h_tokens=usage.cache_write_1h,
        cost_usd=cost_usd(model, usage, batch=batch),
    )
    session.add(row)
    await session.flush()
    return row


def local_day_start(clock: Clock) -> datetime:
    return local_to_utc(clock.today(), datetime.min.time(), clock.zone)


async def spent_since(session: AsyncSession, since: datetime) -> float:
    total = await session.scalar(
        select(func.coalesce(func.sum(LlmUsage.cost_usd), 0.0)).where(LlmUsage.at >= since)
    )
    return float(total or 0.0)


@dataclass(frozen=True, slots=True)
class BudgetState:
    spent: float
    budget: float
    mode: BudgetMode

    @property
    def pct(self) -> float:
        return 100 * self.spent / self.budget if self.budget else 0.0


def budget_mode(spent: float, budget: float, warn_pct: int) -> BudgetMode:
    if budget <= 0:
        return "normal"
    if spent >= budget * STOP_FACTOR:
        return "stopped"
    if spent >= budget:
        return "haiku_only"
    if spent >= budget * warn_pct / 100:
        return "warn"
    return "normal"


async def budget_state(
    session: AsyncSession, clock: Clock, budget: float, warn_pct: int
) -> BudgetState:
    spent = await spent_since(session, local_day_start(clock))
    return BudgetState(spent, budget, budget_mode(spent, budget, warn_pct))


def next_midnight(clock: Clock) -> datetime:
    return local_to_utc(clock.today() + timedelta(days=1), datetime.min.time(), clock.zone)
