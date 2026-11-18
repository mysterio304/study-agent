"""Custom column types."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from agent.core.timeutil import ensure_utc


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as naive UTC; returns aware UTC. Rejects naive input.

    SQLite has no timezone support, so the invariant "everything in the DB is UTC" is enforced
    here, at the boundary, instead of being a convention every caller must remember.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        return ensure_utc(value).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)
