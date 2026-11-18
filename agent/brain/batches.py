"""Message Batches (50 % cheaper) for non-interactive work: topic discovery now, nightly quiz
generation in M5 (SPEC §9a.6). Submission is recorded in `llm_batches`; the 1-minute tick polls,
records usage at the batch rate and hands results to a per-purpose handler. If a batch is still
running after its deadline, it is cancelled and the caller's on-demand fallback runs instead.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.brain.accounting import record_usage, usage_from_response
from agent.brain.budgets import CALL_SPECS, check_request
from agent.brain.llm import BudgetExceeded, LlmClient, build_params
from agent.config import Settings
from agent.core.clock import Clock
from agent.db.models import LlmBatch

log = logging.getLogger(__name__)

ResultHandler = Callable[[LlmBatch, dict[str, Any | None]], Awaitable[None]]
FallbackHandler = Callable[[LlmBatch], Awaitable[None]]


class BatchBackend(Protocol):
    """Subset of `AsyncAnthropic().messages.batches`."""

    async def create(self, *, requests: list[dict[str, Any]]) -> Any: ...
    async def retrieve(self, batch_id: str) -> Any: ...
    async def cancel(self, batch_id: str) -> Any: ...
    def results(self, batch_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class BatchItem:
    custom_id: str
    call_type: str
    system: str | list[dict[str, Any]]
    messages: list[dict[str, Any]]
    json_schema: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Purpose:
    on_results: ResultHandler
    on_timeout: FallbackHandler | None = None
    deadline: timedelta = timedelta(minutes=20)


class BatchRunner:
    def __init__(
        self,
        backend: BatchBackend,
        llm: LlmClient,
        sessionmaker: async_sessionmaker,
        settings: Settings,
        clock: Clock,
    ) -> None:
        self._b, self._llm, self._sm, self._settings, self._clock = (
            backend,
            llm,
            sessionmaker,
            settings,
            clock,
        )
        self._purposes: dict[str, Purpose] = {}

    def register(self, name: str, purpose: Purpose) -> None:
        self._purposes[name] = purpose

    async def submit(
        self, purpose: str, items: list[BatchItem], meta: dict[str, Any] | None = None
    ) -> LlmBatch:
        mode = (await self._llm.budget()).mode
        if mode == "stopped":
            raise BudgetExceeded("LLM budget exhausted")
        requests = []
        for it in items:
            spec = CALL_SPECS[it.call_type]
            model = self._llm.model_for("cheap" if mode == "haiku_only" else spec.role)
            check_request(spec, model, system=it.system, tools=None, messages=it.messages)
            params = build_params(
                model,
                spec,
                system=it.system,
                messages=it.messages,
                tools=None,
                ttl=self._settings.cache_ttl,
                json_schema=it.json_schema,
                automatic_cache=False,
            )
            requests.append({"custom_id": it.custom_id, "params": params})
        created = await self._b.create(requests=requests)
        async with self._sm() as s:
            row = LlmBatch(
                anthropic_batch_id=created.id,
                purpose=purpose,
                status="submitted",
                request_count=len(requests),
                meta_json=meta or {},
                submitted_at=self._clock.now(),
            )
            s.add(row)
            await s.commit()
        log.info("Submitted batch %s (%s, %d requests)", created.id, purpose, len(requests))
        return row

    async def poll(self) -> None:
        """Called by the tick: collect finished batches, fall back on overdue ones."""
        async with self._sm() as s:
            pending = list(
                (await s.scalars(select(LlmBatch).where(LlmBatch.status == "submitted"))).all()
            )
        for row in pending:
            purpose = self._purposes.get(row.purpose)
            if purpose is None:
                continue
            try:
                await self._poll_one(row, purpose)
            except Exception:  # one bad batch must not stop the tick
                log.exception("Polling batch %s failed", row.anthropic_batch_id)

    async def _poll_one(self, row: LlmBatch, purpose: Purpose) -> None:
        remote = await self._b.retrieve(row.anthropic_batch_id)
        if remote.processing_status == "ended":
            # Collect (usage is recorded in one transaction) and mark consumed BEFORE running the
            # handler, so a failing handler can never cause a re-collect / double-billed usage.
            results = await self._collect(row)
            await self._set_status(row, "consumed")
            await purpose.on_results(row, results)
            return
        if self._clock.now() - row.submitted_at > purpose.deadline and purpose.on_timeout:
            log.warning(
                "Batch %s overdue; cancelling and running on demand", row.anthropic_batch_id
            )
            await self._b.cancel(row.anthropic_batch_id)
            await self._set_status(row, "failed")
            await purpose.on_timeout(row)

    async def _collect(self, row: LlmBatch) -> dict[str, Any | None]:
        stream = self._b.results(row.anthropic_batch_id)
        if inspect.isawaitable(stream):
            stream = await stream
        out: dict[str, Any | None] = {}
        async with self._sm() as s:
            async for item in stream:
                if item.result.type == "succeeded":
                    msg = item.result.message
                    out[item.custom_id] = msg
                    await record_usage(
                        s,
                        self._clock,
                        model=msg.model,
                        purpose=row.purpose,
                        usage=usage_from_response(msg.usage),
                        batch=True,
                        prefix_key=f"batch:{row.purpose}",
                    )
                else:
                    out[item.custom_id] = None
            await s.commit()
        return out

    async def _set_status(self, row: LlmBatch, status: str) -> None:
        async with self._sm() as s:
            db_row = await s.get(LlmBatch, row.id)
            db_row.status = status
            db_row.ended_at = self._clock.now()
            await s.commit()
