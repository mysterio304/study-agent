"""Composition root: builds long-lived dependencies once and hands them to the entrypoints."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agent.canvas.api import CanvasAPI, CanvasSource
from agent.canvas.client import CanvasClient
from agent.canvas.fake import FakeCanvasSource
from agent.canvas.sync import sync_courses
from agent.config import Settings
from agent.core.clock import Clock, SystemClock
from agent.core.logging import setup_logging
from agent.db.migrate import upgrade_to_head
from agent.db.models import SyncState
from agent.db.session import make_engine, make_sessionmaker
from agent.prep.seed import seed_exams

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker
    clock: Clock
    canvas: CanvasSource
    dry_run: bool

    async def sync_canvas(self) -> None:
        """Scheduled job. Failures are recorded, never raised into the scheduler."""
        try:
            async with self.sessionmaker() as session:
                await sync_courses(session, self.canvas, self.clock)
                await session.commit()
        except Exception as exc:
            log.exception("Canvas sync failed")
            async with self.sessionmaker() as session:
                state = await session.get(SyncState, "courses") or SyncState(scope="courses")
                state.last_error = f"{type(exc).__name__}: {exc}"[:500]
                state.consecutive_failures = (state.consecutive_failures or 0) + 1
                session.add(state)
                await session.commit()


def _secrets(settings: Settings) -> list[str]:
    values = [
        settings.telegram_bot_token,
        settings.canvas_token,
        settings.anthropic_api_key,
        settings.elevenlabs_api_key,
    ]
    return [v.get_secret_value() for v in values if v is not None]


@asynccontextmanager
async def build_app(settings: Settings, *, dry_run: bool) -> AsyncIterator[App]:
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
        yield App(settings, engine, sm, SystemClock(settings.zone), canvas, dry_run)
    finally:
        if client is not None:
            await client.aclose()
        await engine.dispose()
