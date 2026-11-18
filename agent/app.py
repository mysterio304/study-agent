"""Composition root: builds long-lived dependencies once and hands them to the entrypoints."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from sqlalchemy import select
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
from agent.db.models import CourseFile
from agent.db.session import make_engine, make_sessionmaker
from agent.files.indexer import index_pending
from agent.flows import Services, on_topics_ready, propose_importance, quiet_range
from agent.notify.dispatch import summarise
from agent.prep.discovery import TopicDiscovery
from agent.prep.quizzes import QuizBank
from agent.prep.seed import seed_exams
from agent.telegram.notifier import ConsoleNotifier, Notifier
from agent.voice.elevenlabs import ElevenLabs
from agent.voice.service import VoiceService

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
    _closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list, repr=False)

    async def aclose(self) -> None:
        for close in self._closers:
            await close()

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

    async def index_files(self) -> None:
        """Scheduled job: download + index a few pending course files (exam courses only)."""
        try:
            done = await index_pending(
                self.sessionmaker, self.canvas, Path(self.settings.data_dir), self.clock
            )
        except Exception:
            log.exception("File indexing failed")
            return
        if done:
            log.info(
                "Indexed %d files: %s", len(done), ", ".join(f"{r.name} ({r.state})" for r in done)
            )
        indexed = [r.file_id for r in done if r.state == "indexed"]
        if self.svc is None or not indexed:
            return
        async with self.sessionmaker() as s:
            rows = await s.scalars(
                select(CourseFile).where(
                    CourseFile.id.in_(indexed), CourseFile.kind == "past_paper"
                )
            )
            courses = {f.course_id for f in rows.all()}
        for course_id in courses:  # new past papers → topic importance proposal
            await propose_importance(self.svc, course_id)

    def build_services(self, notifier: Notifier | None = None) -> Services:
        """Wire LLM, agent, batches and discovery. Without an API key the agent is disabled and
        topic discovery uses fixtures."""
        notifier = notifier or ConsoleNotifier()
        svc = Services(
            self.settings,
            self.sessionmaker,
            self.clock,
            self.canvas,
            notifier,
            data_dir=Path(self.settings.data_dir),
        )
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
            svc.quizzes = QuizBank(self.sessionmaker, svc.llm, svc.batches, self.clock)

            async def diagnostic_ready(meta: dict) -> None:
                from agent.prep.diagnostic import on_bank_ready  # runtime import: avoids a cycle

                await on_bank_ready(svc, meta)

            svc.quizzes.on_ready.append(diagnostic_ready)

        if self.settings.elevenlabs_api_key is not None:
            el = ElevenLabs(
                self.settings.elevenlabs_api_key.get_secret_value(),
                stt_model=self.settings.elevenlabs_stt_model,
                tts_model=self.settings.elevenlabs_tts_model,
                voice_id=self.settings.elevenlabs_voice_id,
            )
            self._closers.append(el.aclose)
            svc.voice = VoiceService(
                el,
                self.sessionmaker,
                self.clock,
                tts_daily_char_limit=self.settings.tts_daily_char_limit,
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
    app = App(settings, engine, sm, clock or SystemClock(settings.zone), canvas, dry_run)
    try:
        yield app
    finally:
        await app.aclose()
        if client is not None:
            await client.aclose()
        await engine.dispose()
