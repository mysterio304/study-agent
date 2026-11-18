"""Composition root: builds long-lived dependencies once and hands them to the entrypoints."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anthropic
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agent.brain.accounting import BudgetState
from agent.brain.agent import Agent
from agent.brain.batches import BatchRunner
from agent.brain.llm import LlmClient
from agent.canvas.api import CanvasAPI, CanvasSource
from agent.canvas.client import CanvasClient
from agent.canvas.fake import FakeCanvasSource
from agent.canvas.sync_runner import CanvasSync
from agent.config import Settings
from agent.core.clock import Clock, SystemClock
from agent.core.logging import setup_logging
from agent.db.migrate import upgrade_to_head
from agent.db.session import make_engine, make_sessionmaker
from agent.flows import Services, on_topics_ready, quiet_range
from agent.notify.dispatch import summarise
from agent.prep.discovery import TopicDiscovery
from agent.prep.seed import seed_exams
from agent.telegram.notifier import ConsoleNotifier, Notifier

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker
    clock: Clock
    canvas: CanvasSource
    dry_run: bool
    svc: Services | None = None
    _sync: CanvasSync | None = field(default=None, repr=False)

    async def sync_canvas(self) -> None:
        """Scheduled job: full Canvas sync → events → outbox. Never raises into the scheduler
        (per-scope failures are recorded in `sync_state` by CanvasSync)."""
        if self._sync is None:
            svc = self.svc
            self._sync = CanvasSync(
                self.sessionmaker,
                self.canvas,
                self.clock,
                quiet=(lambda: quiet_range(svc)) if svc else None,
                summarise=(lambda text: summarise(svc.llm, text)) if svc else None,
            )
        try:
            report = await self._sync.run()  # notifications are queued atomically with state
        except Exception:
            log.exception("Canvas sync failed")
            return
        if report.failed_scopes:
            log.warning("Canvas scopes failed: %s", ", ".join(report.failed_scopes[:5]))

    def build_services(self, notifier: Notifier | None = None) -> Services:
        """Wire LLM, agent, batches and discovery. Without an API key the agent is disabled and
        topic discovery uses fixtures."""
        notifier = notifier or ConsoleNotifier()
        svc = Services(self.settings, self.sessionmaker, self.clock, self.canvas, notifier)
        if self.settings.anthropic_api_key is not None:
            client = anthropic.AsyncAnthropic(
                api_key=self.settings.anthropic_api_key.get_secret_value()
            )

            async def warn(state: BudgetState) -> None:
                if state.mode == "warn":
                    await notifier.send(
                        f"⚠️ LLM: потрачено ${state.spent:.2f} из ${state.budget:.2f} за сегодня."
                    )
                else:
                    await notifier.send(
                        "⚠️ Дневной лимит LLM достигнут — до полуночи работаю только на Haiku и шаблонах."
                    )

            svc.llm = LlmClient(
                client.beta.messages.create,
                self.sessionmaker,
                self.settings,
                self.clock,
                on_budget_warning=warn,
            )
            svc.agent = Agent(svc.llm, self.sessionmaker, self.settings, self.clock)
            svc.batches = BatchRunner(
                client.messages.batches, svc.llm, self.sessionmaker, self.settings, self.clock
            )

        async def ready(exam_id: int) -> None:
            await on_topics_ready(svc, exam_id)

        svc.discovery = TopicDiscovery(
            self.sessionmaker, self.canvas, svc.llm, svc.batches, self.clock, ready
        )
        self.svc = svc
        return svc


def _secrets(settings: Settings) -> list[str]:
    values = [
        settings.telegram_bot_token,
        settings.canvas_token,
        settings.anthropic_api_key,
        settings.elevenlabs_api_key,
    ]
    return [v.get_secret_value() for v in values if v is not None]


@asynccontextmanager
async def build_app(
    settings: Settings, *, dry_run: bool, clock: Clock | None = None
) -> AsyncIterator[App]:
    setup_logging(settings.log_level, _secrets(settings))
    if dry_run:
        settings = settings.model_copy(
            update={"database_url": "sqlite+aiosqlite:///data/dry_run.db"}
        )
    engine = make_engine(settings.database_url)
    await upgrade_to_head(engine)
    sm = make_sessionmaker(engine)
    async with sm() as session:
        if created := await seed_exams(session):
            log.info("Seeded %d exams", created)
        await session.commit()

    client: CanvasClient | None = None
    if dry_run:
        canvas: CanvasSource = FakeCanvasSource()
    else:
        assert settings.canvas_base_url and settings.canvas_token
        client = CanvasClient(settings.canvas_base_url, settings.canvas_token.get_secret_value())
        canvas = CanvasAPI(client)
    try:
        yield App(settings, engine, sm, clock or SystemClock(settings.zone), canvas, dry_run)
    finally:
        if client is not None:
            await client.aclose()
        await engine.dispose()
