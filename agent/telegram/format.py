"""Russian-language formatting helpers for Telegram messages."""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def fmt_dt(dt: datetime, zone: ZoneInfo, *, weekday: bool = True) -> str:
    """'пн 8 дек, 09:30' in local time."""
    local = dt.astimezone(zone)
    base = f"{local.day} {_MONTHS[local.month - 1]}, {local:%H:%M}"
    return f"{_WEEKDAYS[local.weekday()]} {base}" if weekday else base


def h(text: object) -> str:
    """HTML-escape untrusted text (course names etc.) for parse_mode=HTML."""
    return escape(str(text), quote=False)
