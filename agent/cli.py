"""Operator/demo CLI: `python -m agent.cli <command> [--dry-run]`.

Commands:
  sync   — sync Canvas courses into the DB and list them
  map    — show the suggested exam → course mapping
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from agent.app import App, build_app
from agent.config import get_settings
from agent.prep.mapping_service import exams_in_order, list_courses, suggestions_for_unconfirmed
from agent.telegram.format import fmt_dt


def _out(line: str = "") -> None:
    sys.stdout.write(line + "\n")


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


COMMANDS = {"sync": demo_sync, "map": demo_mapping}


async def _amain(command: str, dry_run: bool) -> None:
    settings = get_settings()
    if not dry_run:
        settings.require_live()
    async with build_app(settings, dry_run=dry_run) as app:
        if command == "map":
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
