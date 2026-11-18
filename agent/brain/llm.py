"""Single entry point for every Claude call: model resolution, request shaping, guards,
prompt caching and accounting (SPEC §9a). Nothing else in the codebase calls the API directly.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.brain.accounting import BudgetState, budget_state, record_usage, usage_from_response
from agent.brain.budgets import CALL_SPECS, CallSpec, ModelRole, check_request
from agent.config import Settings
from agent.core.clock import Clock

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """Daily LLM budget exhausted (≥150 %): only templates until midnight HKT."""


class Backend(Protocol):
    """`AsyncAnthropic().beta.messages.create` (beta endpoint for refusal fallbacks) or a test
    double."""

    def __call__(self, **params: Any) -> Awaitable[Any]: ...


@dataclass(frozen=True, slots=True)
class CallResult:
    message: Any
    model: str
    spec: CallSpec
    cost_usd: float


def thinking_params(model: str, thinking: str) -> dict[str, Any]:
    """Map our thinking level to per-model API params.

    Haiku 5.5: thinking can be disabled (allowed at effort ≤ high). Sonnet 5.5: `disabled` is
    a 400, so 'off' uses `between_tools` (thinking only between tool calls).
    """
    if thinking == "off":
        if "sonnet" in model:
            return {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}
        return {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": thinking}}


REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def cache_control(ttl: str) -> dict[str, str]:
    return {"type": "ephemeral", "ttl": ttl}


def build_params(
    model: str,
    spec: CallSpec,
    *,
    system: Sequence[dict[str, Any]] | str,
    messages: list[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None,
    ttl: str,
    max_tokens: int | None = None,
    json_schema: dict[str, Any] | None = None,
    automatic_cache: bool = True,
) -> dict[str, Any]:
    """Request parameters shared by live calls and Batches."""
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens or spec.max_tokens,
        "system": system,
        "messages": messages,
        **thinking_params(model, spec.thinking),
    }
    if automatic_cache:  # automatic breakpoint: caches the growing conversation tail
        params["cache_control"] = cache_control(ttl)
    if tools:
        params["tools"] = list(tools)
    if json_schema is not None:
        params["output_config"] = {
            **params["output_config"],
            "format": {"type": "json_schema", "schema": json_schema},
        }
    if automatic_cache and "sonnet" in model:
        # Server-side refusal fallback (live calls only; Batches reject `fallbacks`, and Haiku
        # has none — a Haiku refusal is retried on Sonnet by the agent loop instead).
        params["betas"] = [REFUSAL_FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


class LlmClient:
    def __init__(
        self,
        backend: Backend,
        sessionmaker: async_sessionmaker,
        settings: Settings,
        clock: Clock,
        *,
        on_budget_warning: Callable[[BudgetState], Awaitable[None]] | None = None,
    ) -> None:
        self._backend = backend
        self._sm = sessionmaker
        self._settings = settings
        self._clock = clock
        self._on_warn = on_budget_warning
        self._warned_day: Any = None

    def model_for(self, role: ModelRole) -> str:
        return self._settings.smart_model if role == "smart" else self._settings.cheap_model

    async def budget(self) -> BudgetState:
        async with self._sm() as s:
            return await budget_state(
                s,
                self._clock,
                self._settings.daily_llm_budget_usd,
                self._settings.llm_budget_warn_pct,
            )

    async def call(
        self,
        call_type: str,
        *,
        system: Sequence[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        role: ModelRole | None = None,
        route_reason: str | None = None,
        prefix_key: str | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> CallResult:
        """One Messages API call with every guard applied. Raises BudgetExceeded / PromptTooLarge."""
        spec = CALL_SPECS[call_type]
        state = await self.budget()
        if state.mode == "stopped":
            raise BudgetExceeded(f"Spent ${state.spent:.2f} of ${state.budget:.2f} today")
        await self._maybe_warn(state)
        wanted = role or spec.role
        if state.mode == "haiku_only":
            wanted = "cheap"
        model = self.model_for(wanted)
        check_request(spec, model, system=system, tools=tools, messages=messages)

        params = build_params(
            model,
            spec,
            system=system,
            messages=messages,
            tools=tools,
            ttl=self._settings.cache_ttl,
            max_tokens=max_tokens,
            json_schema=json_schema,
        )
        message = await self._backend(**params)
        usage = usage_from_response(getattr(message, "usage", None))
        served_by = getattr(message, "model", None) or model  # may differ after a fallback
        async with self._sm() as s:
            row = await record_usage(
                s,
                self._clock,
                model=served_by,
                purpose=call_type,
                usage=usage,
                route_reason=route_reason,
                prefix_key=prefix_key or f"{model}:{call_type}",
            )
            await s.commit()
        if getattr(message, "stop_reason", None) == "max_tokens":
            log.warning("%s hit max_tokens (%s)", call_type, params["max_tokens"])
        return CallResult(message, model, spec, row.cost_usd)

    async def _maybe_warn(self, state: BudgetState) -> None:
        if (
            state.mode in ("warn", "haiku_only")
            and self._on_warn
            and self._warned_day != (self._clock.today(), state.mode)
        ):
            self._warned_day = (self._clock.today(), state.mode)
            await self._on_warn(state)


def text_of(message: Any) -> str:
    return "".join(
        getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text"
    ).strip()


def block_to_param(block: Any) -> dict[str, Any]:
    """Serialise a response content block to pass it back unchanged (thinking blocks included)."""
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return {k: v for k, v in vars(block).items() if v is not None}
