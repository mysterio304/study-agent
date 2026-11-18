"""Operator/demo CLI: `python -m agent.cli <command> [--dry-run]`.

Commands:
  sync   — sync Canvas courses into the DB and list them
  map    — show the suggested exam → course mapping
  plan   — (dry-run) full M2 walkthrough: mapping → topics → self-rating → first joint plan →
           block pings → a missed block → nightly replan proposal → undo
"""

from __future__ import annotations

import argparse
import asyncio
import html
import re
import sys
from collections import Counter
from datetime import datetime, time, timedelta

from sqlalchemy import select

from agent.app import App, build_app
from agent.config import get_settings
from agent.core.clock import FixedClock
from agent.core.timeutil import local_to_utc
from agent.db.models import Base, Proposal
from agent.flows import Services, apply_choice, continue_setup, on_topics_ready
from agent.notify import jobs
from agent.planner.render import fmt_day, group_by_day, short_exam
from agent.planner.service import future_blocks, load_inputs, propose_undo
from agent.prep import topics_service as ts
from agent.prep.discovery import TopicDiscovery
from agent.prep.mapping_service import (
    confirm_mapping,
    exams_in_order,
    list_courses,
    suggestions_for_unconfirmed,
)
from agent.telegram.cards import day_view, progress_view
from agent.telegram.format import fmt_dt
from agent.telegram.notifier import ConsoleNotifier

DEMO_LEVELS = {1: 1, 2: 1, 3: 2, 4: 2}  # self-rating used by the demo (per exam id)


def _out(line: str = "") -> None:
    sys.stdout.write(html.unescape(re.sub(r"</?(b|i|code)>", "", line)) + "\n")


def _section(title: str) -> None:
    _out(f"\n{'═' * 70}\n{title}\n{'═' * 70}")


async def demo_sync(app: App) -> None:
    await app.sync_canvas()
    async with app.sessionmaker() as session:
        courses = await list_courses(session)
    _out(f"Synced {len(courses)} active courses:")
    for c in courses:
        score = (
            f"{c.current_score:.1f}% ({c.current_grade})" if c.current_score is not None else "—"
        )
        _out(f"  [{c.id}] {c.name:<45} {score}")


async def demo_mapping(app: App) -> None:
    async with app.sessionmaker() as session:
        exams = await exams_in_order(session)
        pending = await suggestions_for_unconfirmed(session)
    _out("Finals (HKT):")
    for e in exams:
        weight = f"{e.final_weight:.0%}" if e.final_weight is not None else "weight unknown"
        when = fmt_dt(e.starts_at, app.settings.zone)
        _out(f"  {when:<20} {e.title}  [{weight}, target {e.target_grade}]")
    _out()
    _out("Suggested exam → course mapping (confirm in Telegram via /start):")
    for exam, s in pending:
        target = f"{s.course_name}  (score {s.score:.2f})" if s.course_id else "NO MATCH"
        flag = "  ⚠ ambiguous" if s.ambiguous else ""
        _out(f"  {exam.title:<45} → {target}{flag}")


async def _plan_shape(svc: Services) -> None:
    async with svc.sessionmaker() as s:
        inputs = await load_inputs(s, svc.settings, svc.clock)
        blocks = await future_blocks(s, svc.clock.now() - timedelta(hours=1))
    for day, items in group_by_day(blocks, svc.settings.zone).items():
        counts = Counter(short_exam(inputs.exam_names[b.exam_id]) for b in items)
        kinds = Counter(b.kind for b in items)
        _out(f"  {fmt_day(day):<11} {len(items):>2} блоков  {dict(counts)}  {dict(kinds)}")


def _set_local(clock: FixedClock, day_offset: int, at: time) -> None:
    target = local_to_utc(clock.today() + timedelta(days=day_offset), at, clock.zone)
    clock.advance(seconds=(target - clock.now()).total_seconds())


async def demo_plan(app: App) -> None:
    clock = app.clock
    assert isinstance(clock, FixedClock), "demo_plan runs on a fixed clock"
    svc = app.build_services(ConsoleNotifier())
    svc.llm = svc.agent = svc.batches = None  # dry-run: no API calls

    async def ready(exam_id: int) -> None:
        await on_topics_ready(svc, exam_id)

    svc.discovery = TopicDiscovery(app.sessionmaker, app.canvas, None, None, clock, ready)

    _section("1. Canvas sync + exam ↔ course mapping (auto-confirmed for the demo)")
    await app.sync_canvas()
    async with app.sessionmaker() as s:
        for exam, sug in await suggestions_for_unconfirmed(s):
            await confirm_mapping(s, exam.id, sug.course_id)
            _out(f"  {exam.title:<45} → {sug.course_name}")
        await s.commit()

    _section("2. Topic discovery (fixture instead of a Sonnet batch) — review cards")
    await svc.discovery.start()

    _section("3. Topics accepted, self-rating (demo levels) → first plan proposal")
    async with app.sessionmaker() as s:
        for exam_id, level in DEMO_LEVELS.items():
            await ts.accept_topics(s, exam_id)
            await ts.rate_rest_of_exam(s, exam_id, level)
        await s.commit()
    await continue_setup(svc)

    _section("4. Student taps ✅ Принять")
    async with app.sessionmaker() as s:
        pid = await s.scalar(select(Proposal.id).where(Proposal.status == "pending"))
    _out("  " + await apply_choice(svc, pid, "A"))
    _out("\nJoint plan shape (blocks per day per exam):")
    await _plan_shape(svc)

    _section("5. /today and /progress (templates, no LLM)")
    async with app.sessionmaker() as s:
        _out(await day_view(s, app.settings, clock.today()))
        _out("\n" + await progress_view(s))

    _section("6. 09:00 block ping → no answer → re-ping 09:15 → missed at 10:25 → auto-move")
    _set_local(clock, 0, time(9, 0))
    await jobs.tick(svc)
    clock.advance(minutes=15)
    await jobs.tick(svc)
    clock.advance(minutes=70)
    await jobs.tick(svc)

    _section("7. Nightly replan 03:30 → proposal waits for the 08:30 morning digest")
    _set_local(clock, 1, time(3, 30))
    await jobs.nightly(svc)
    _set_local(clock, 0, time(8, 30))
    await jobs.morning(svc)

    _section("8. /undo → one-tap proposal to restore the previous version")
    async with app.sessionmaker() as s:
        inputs = await load_inputs(s, app.settings, clock)
        p = await propose_undo(s, inputs, clock)
        await s.commit()
    _out(f"  undo proposal: {'#' + str(p.id) if p else 'none'}")


COMMANDS = {"sync": demo_sync, "map": demo_mapping, "plan": demo_plan}


async def _reset_dry_run_db(app: App) -> None:
    """The walkthrough starts from a clean slate each time (dry-run DB only)."""
    assert app.dry_run
    async with app.engine.begin() as conn:
        exams = Base.metadata.tables["exams"]
        await conn.execute(exams.update().values(course_id=None, mapping_confirmed=False))
        for table in reversed(Base.metadata.sorted_tables):
            if table.name != "exams":
                await conn.execute(table.delete())


async def _amain(command: str, dry_run: bool) -> None:
    settings = get_settings()
    if not dry_run:
        settings.require_live()
    clock = None
    if command == "plan":
        if not dry_run:
            raise SystemExit("The plan walkthrough only runs with --dry-run.")
        start = datetime.combine(
            datetime.now(settings.zone).date() + timedelta(days=1), time(7, 0), settings.zone
        )
        clock = FixedClock(start, settings.zone)
        settings = settings.model_copy(update={"anthropic_api_key": None, "log_level": "WARNING"})
    async with build_app(settings, dry_run=dry_run, clock=clock) as app:
        if command == "plan":
            await _reset_dry_run_db(app)
        elif command == "map":
            await app.sync_canvas()
        await COMMANDS[command](app)


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent.cli")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(_amain(args.command, args.dry_run))


if __name__ == "__main__":
    main()
