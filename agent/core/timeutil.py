"""Time helpers. Rule: store and compute in UTC, convert to local (HKT) only at the edges."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def parse_hhmm(value: str) -> time:
    """Parse 'HH:MM' into a `time`. Raises ValueError on bad input."""
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Expected HH:MM, got {value!r}") from exc


@dataclass(frozen=True, slots=True)
class TimeRange:
    """A daily local-time range; may wrap past midnight (23:00-07:00 wraps)."""

    start: time
    end: time

    @property
    def wraps_midnight(self) -> bool:
        return self.end <= self.start

    def contains(self, t: time) -> bool:
        if self.wraps_midnight:
            return t >= self.start or t < self.end
        return self.start <= t < self.end


def parse_time_range(value: str) -> TimeRange:
    """Parse 'HH:MM-HH:MM'."""
    try:
        start, end = value.split("-")
    except ValueError as exc:
        raise ValueError(f"Expected HH:MM-HH:MM, got {value!r}") from exc
    rng = TimeRange(parse_hhmm(start), parse_hhmm(end))
    if rng.start == rng.end:
        raise ValueError(f"Empty time range {value!r}")
    return rng


def ensure_utc(dt: datetime) -> datetime:
    """Return an aware UTC datetime; reject naive datetimes to catch timezone bugs early."""
    if dt.tzinfo is None:
        raise ValueError("Naive datetime is not allowed; attach a timezone first")
    return dt.astimezone(UTC)


def local_to_utc(day: date, t: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, t, tzinfo=zone).astimezone(UTC)


def to_local(dt: datetime, zone: ZoneInfo) -> datetime:
    return ensure_utc(dt).astimezone(zone)


def parse_iso_utc(value: str | None) -> datetime | None:
    """Parse Canvas-style ISO-8601 timestamps ('2026-11-20T15:59:59Z') into aware UTC."""
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def days_between(a: date, b: date) -> int:
    return (b - a).days


def daterange(start: date, end_inclusive: date):
    d = start
    while d <= end_inclusive:
        yield d
        d += timedelta(days=1)
