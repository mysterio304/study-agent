from datetime import date, time, timedelta

from agent.planner.model import Interval
from agent.planner.slots import day_capacity_blocks, day_slots
from tests.planner.conftest import HKT, at


def local_times(slots):
    return [(s.start.astimezone(HKT).time(), s.end.astimezone(HKT).time()) for s in slots]


def test_regular_day_has_capped_block_rhythm(cfg, exams) -> None:
    day = date(2026, 11, 19)  # Thursday, 19 days before the first exam
    slots = day_slots(day, cfg, exams, busy=[])
    assert day_capacity_blocks(day, cfg, exams) == 7  # floor(6.5h / 50min)
    assert len(slots) == 7
    times = local_times(slots)
    assert times[:4] == [
        (time(9, 0), time(9, 50)),
        (time(10, 0), time(10, 50)),
        (time(11, 0), time(11, 50)),
        (time(12, 20), time(13, 10)),  # long break after 3 blocks
    ]
    assert all(s.end - s.start == timedelta(minutes=50) for s in slots)


def test_sunday_is_lighter_unless_exam_is_close(cfg, exams) -> None:
    assert day_capacity_blocks(date(2026, 11, 22), cfg, exams) == 3  # Sunday, 50 %
    assert day_capacity_blocks(date(2026, 12, 6), cfg, exams) == 10  # Sunday but exam in 2 days


def test_final_week_and_between_exams_caps(cfg, exams) -> None:
    assert day_capacity_blocks(date(2026, 12, 3), cfg, exams) == 10  # 8.5h
    assert day_capacity_blocks(date(2026, 12, 10), cfg, exams) == 9  # 7.5h
    assert day_capacity_blocks(date(2026, 12, 8), cfg, exams) == 3  # exam day: light
    assert day_capacity_blocks(date(2026, 12, 14), cfg, exams) == 3  # CV at 14:00: morning review
    assert day_capacity_blocks(date(2026, 12, 15), cfg, exams) == 0  # all exams done


def test_protected_sleep_before_morning_exam(cfg, exams) -> None:
    slots = day_slots(date(2026, 12, 7), cfg, exams, busy=[])
    assert slots and max(s.end for s in slots) <= at(date(2026, 12, 7), 21, 0)


def test_no_blocks_during_exam_or_buffers(cfg, exams) -> None:
    dec8 = day_slots(date(2026, 12, 8), cfg, exams, busy=[])
    forbidden = Interval(at(date(2026, 12, 8), 8, 30), at(date(2026, 12, 8), 12, 0))
    assert dec8 and not any(Interval(s.start, s.end).overlaps(forbidden) for s in dec8)
    dec9 = day_slots(date(2026, 12, 9), cfg, exams, busy=[])
    assert dec9 and max(s.end for s in dec9) <= at(date(2026, 12, 9), 17, 30)


def test_busy_intervals_are_skipped(cfg, exams) -> None:
    day = date(2026, 11, 20)
    lecture = Interval(at(day, 10, 0), at(day, 13, 0))
    slots = day_slots(day, cfg, exams, busy=[lecture])
    assert not any(Interval(s.start, s.end).overlaps(lecture) for s in slots)
    assert len(slots) == 7


def test_not_before_drops_past_slots(cfg, exams) -> None:
    day = date(2026, 11, 19)
    slots = day_slots(day, cfg, exams, busy=[], not_before=at(day, 15, 5))
    assert slots and min(s.start for s in slots) >= at(day, 15, 5)
