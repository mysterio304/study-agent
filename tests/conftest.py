"""Shared fixtures: a migrated temporary SQLite DB and a frozen clock."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.core.clock import FixedClock
from agent.db.migrate import upgrade_to_head
from agent.db.session import make_engine, make_sessionmaker

HKT = ZoneInfo("Asia/Hong_Kong")


@pytest.fixture
async def engine(tmp_path):
    eng = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await upgrade_to_head(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(datetime(2026, 11, 18, 20, 0, tzinfo=HKT), HKT)
