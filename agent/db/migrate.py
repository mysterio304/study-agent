"""Run Alembic migrations programmatically (used at startup and in tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "agent" / "db" / "migrations"))
    cfg.attributes["configure_logger"] = False
    return cfg


async def upgrade_to_head(engine: AsyncEngine) -> None:
    """Upgrade the database behind `engine` to the latest revision."""
    cfg = _alembic_config()

    def _run(sync_conn) -> None:  # type: ignore[no-untyped-def]
        cfg.attributes["connection"] = sync_conn
        command.upgrade(cfg, "head")

    async with engine.begin() as conn:
        await conn.run_sync(_run)
