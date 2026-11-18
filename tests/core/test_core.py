import logging
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from agent.config import ConfigError, Settings
from agent.core.clock import FixedClock
from agent.core.logging import RedactingFilter
from agent.core.timeutil import (
    daterange,
    ensure_utc,
    local_to_utc,
    parse_hhmm,
    parse_iso_utc,
    parse_time_range,
    to_local,
)

HKT = ZoneInfo("Asia/Hong_Kong")


# --- time -------------------------------------------------------------------------------------


def test_parse_time_range_and_wraparound() -> None:
    quiet = parse_time_range("00:30-08:00")
    assert not quiet.wraps_midnight
    assert quiet.contains(time(3, 0)) and not quiet.contains(time(8, 0))
    night = parse_time_range("23:00-07:00")
    assert night.wraps_midnight
    assert night.contains(time(23, 30)) and night.contains(time(6, 59))
    assert not night.contains(time(12, 0))


@pytest.mark.parametrize("bad", ["8", "25:00x", "", "10:00-10:00", "1000-1100"])
def test_bad_time_inputs_raise(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_time_range(bad) if "-" in bad else parse_hhmm(bad)


def test_utc_round_trip_and_naive_rejection() -> None:
    utc = local_to_utc(date(2026, 12, 8), time(9, 30), HKT)
    assert utc == datetime(2026, 12, 8, 1, 30, tzinfo=UTC)
    assert to_local(utc, HKT).hour == 9
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 1, 1))


def test_parse_iso_utc_handles_canvas_format() -> None:
    assert parse_iso_utc("2026-11-20T15:59:59Z") == datetime(2026, 11, 20, 15, 59, 59, tzinfo=UTC)
    assert parse_iso_utc(None) is None


def test_daterange_inclusive() -> None:
    assert list(daterange(date(2026, 12, 8), date(2026, 12, 10))) == [
        date(2026, 12, 8),
        date(2026, 12, 9),
        date(2026, 12, 10),
    ]


def test_fixed_clock_local_date_and_advance() -> None:
    clock = FixedClock(datetime(2026, 11, 18, 23, 30, tzinfo=HKT), HKT)
    assert clock.today() == date(2026, 11, 18)
    clock.advance(hours=1)
    assert clock.today() == date(2026, 11, 19)
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 1, 1), HKT)


# --- secret redaction -------------------------------------------------------------------------


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


def test_redacts_configured_secrets_and_token_shapes() -> None:
    f = RedactingFilter(["my-canvas-token-value"])
    rec = _record("calling with %s and bot 123456789:%s", "my-canvas-token-value", "A" * 35)
    f.filter(rec)
    out = rec.getMessage()
    assert "my-canvas-token-value" not in out
    assert "A" * 35 not in out
    assert out.count("***") == 2


def test_redacts_bearer_and_anthropic_keys() -> None:
    f = RedactingFilter()
    text = f.redact("Authorization: Bearer abcdefghijklmnop key=sk-ant-api03-XXXXXXXXXXXXXXXX")
    assert "abcdefghijklmnop" not in text and "sk-ant-api03" not in text
    assert "Bearer ***" in text


def test_redacts_exception_text() -> None:
    f = RedactingFilter(["supersecret123"])
    try:
        raise RuntimeError("token supersecret123 rejected")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    f.filter(rec)
    assert "supersecret123" not in (rec.exc_text or "")


# --- config -----------------------------------------------------------------------------------


def test_settings_defaults_and_require_live(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)
    assert s.agent_model == "claude-sonnet-5-5"
    assert s.quiet_hours_range.contains(time(1, 0))
    assert s.morning_time == time(8, 30)
    with pytest.raises(ConfigError) as exc:
        s.require_live()
    assert "CANVAS_TOKEN" in str(exc.value)


def test_settings_validation() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, canvas_base_url="http://insecure.example.edu")
    with pytest.raises(ValueError):
        Settings(_env_file=None, quiet_hours="nonsense")
    s = Settings(_env_file=None, canvas_base_url="https://canvas.example.edu/")
    assert s.canvas_base_url == "https://canvas.example.edu"


def test_secret_values_are_not_in_repr() -> None:
    s = Settings(_env_file=None, canvas_token="very-secret-token")
    assert "very-secret-token" not in repr(s)
