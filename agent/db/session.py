"""Async engine/session factory. SQLite is tuned for a single-process app (WAL, FKs on)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def _ensure_sqlite_dir(url: str) -> None:
    prefix = "sqlite+aiosqlite:///"
    if url.startswith(prefix) and ":memory:" not in url:
        Path(url.removeprefix(prefix)).parent.mkdir(parents=True, exist_ok=True)


def make_engine(url: str) -> AsyncEngine:
    _ensure_sqlite_dir(url)
    engine = create_async_engine(url, future=True)

    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
