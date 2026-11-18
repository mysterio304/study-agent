"""Russian templates for plans and diffs — no LLM involved (SPEC §9a.1)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from zoneinfo import ZoneInfo

from agent.planner.model import PlannedBlock
from agent.telegram.format import h

KIND_RU = {
    "learn": "изучение",
    "practice": "практика",
    "review": "повторение",
    "mock": "mock-экзамен",
}
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def fmt_day(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def short_exam(title: str) -> str:
    """'Computer Networks' → 'Networks'; keeps messages compact."""
    known = {
        "Computer Networks": "Networks",
        "Design and Analysis of Algorithms": "Algorithms",
        "Computer Vision for Interactivity (CS4187)": "CV",
    }
    return known.get(title, title)


def group_by_day(blocks: Sequence[PlannedBlock], zone: ZoneInfo) -> dict[date, list[PlannedBlock]]:
    days: dict[date, list[PlannedBlock]] = defaultdict(list)
    for b in sorted(blocks, key=lambda x: x.start):
        days[b.start.astimezone(zone).date()].append(b)
    return dict(days)


def render_day(
    day: date,
    blocks: Sequence[PlannedBlock],
    zone: ZoneInfo,
    exam_names: Mapping[int, str],
    topic_names: Mapping[int, str],
    statuses: Mapping[PlannedBlock, str] | None = None,
) -> str:
    if not blocks:
        return f"<b>{fmt_day(day)}</b>: блоков нет."
    marks = {"done": "✅", "skipped": "✖️", "started": "▶️", "moved": "↪️"}
    lines = [f"<b>{fmt_day(day)}</b> — {len(blocks)} блоков ({len(blocks) * 50 / 60:.1f} ч)"]
    for b in blocks:
        start = b.start.astimezone(zone)
        end = b.end.astimezone(zone)
        topic = topic_names.get(b.topic_id, "") if b.topic_id else "полный вариант экзамена"
        mark = marks.get((statuses or {}).get(b, ""), "•")
        lines.append(
            f"{mark} {start:%H:%M}–{end:%H:%M} {h(short_exam(exam_names.get(b.exam_id, '?')))}: "
            f"{h(topic)} <i>({KIND_RU[b.kind]})</i>"
        )
    return "\n".join(lines)


def _counts(blocks: Sequence[PlannedBlock], zone: ZoneInfo) -> dict[date, Counter[int]]:
    return {d: Counter(b.exam_id for b in bs) for d, bs in group_by_day(blocks, zone).items()}


def changed_blocks(old: Sequence[PlannedBlock], new: Sequence[PlannedBlock]) -> int:
    """Blocks of the new plan that differ from the old one (time, topic or kind)."""
    before = {(b.start, b.topic_id, b.kind) for b in old}
    return sum(1 for b in new if (b.start, b.topic_id, b.kind) not in before)


def render_diff(
    old: Sequence[PlannedBlock],
    new: Sequence[PlannedBlock],
    zone: ZoneInfo,
    exam_names: Mapping[int, str],
    *,
    max_days: int = 7,
) -> str:
    """Compact per-day diff of block counts per exam, e.g. 'пн 23 ноя: Networks 3→4, CV 1→0'."""
    before, after = _counts(old, zone), _counts(new, zone)
    lines = []
    for d in sorted(set(before) | set(after)):
        b, a = before.get(d, Counter()), after.get(d, Counter())
        parts = [
            f"{h(short_exam(exam_names.get(eid, '?')))} {b[eid]}→{a[eid]}"
            for eid in sorted(set(b) | set(a))
            if b[eid] != a[eid]
        ]
        if parts:
            lines.append(f"{fmt_day(d)}: " + ", ".join(parts))
    header = f"Меняется {changed_blocks(old, new)} из {len(new)} блоков."
    if not lines:
        return header + " Количество блоков по дням то же (меняются темы/порядок)."
    extra = len(lines) - max_days
    shown = lines[:max_days] + ([f"… и ещё {extra} дн."] if extra > 0 else [])
    return header + "\n" + "\n".join(shown)
