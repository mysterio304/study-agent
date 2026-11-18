"""Injectable clock so that time-dependent logic (planner, reminders) is deterministic in tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    zone: ZoneInfo

    def now(self) -> datetime:
        """Current time as an aware UTC datetime."""
        ...

    def today(self) -> date:
        """Current local date."""
        ...


class SystemClock:
    def __init__(self, zone: ZoneInfo) -> None:
        self.zone = zone

    def now(self) -> datetime:
        return datetime.now(UTC)

    def today(self) -> date:
        return self.now().astimezone(self.zone).date()


class FixedClock:
    """A clock frozen at a given instant; `advance()` moves it forward."""

    def __init__(self, at: datetime, zone: ZoneInfo) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs an aware datetime")
        self._at = at.astimezone(UTC)
        self.zone = zone

    def now(self) -> datetime:
        return self._at

    def today(self) -> date:
        return self._at.astimezone(self.zone).date()

    def advance(self, **kwargs: float) -> None:
        from datetime import timedelta

        self._at = self._at + timedelta(**kwargs)
