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
import json
import re
import sys
from collections import Counter
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy import func, select

from agent.app import App, build_app
from agent.canvas.sync import sync_courses
from agent.config import get_settings
from agent.core.clock import FixedClock
from agent.core.timeutil import local_to_utc
from agent.db.models import (
    Base,
    Block,
    Chunk,
    CourseFile,
    Exam,
    MockExam,
    PlanVersion,
    Proposal,
    Quiz,
    SyncState,
    Topic,
)
from agent.flows import Services, apply_choice, continue_setup, on_topics_ready
from agent.grades.service import deadlines_view, grades_view, need_for_exam, render_need
from agent.notify import jobs
from agent.planner.render import fmt_day, group_by_day, short_exam
from agent.planner.service import future_blocks, load_inputs, propose_undo
from agent.prep import mock as mock_mod
from agent.prep import topics_service as ts
from agent.prep.discovery import TopicDiscovery
from agent.prep.mapping_service import (
    confirm_mapping,
    exams_in_order,
    list_courses,
    suggestions_for_unconfirmed,
)
from agent.prep.quizzes import record_answer, store_questions
from agent.telegram.cards import day_view, progress_view
from agent.telegram.format import fmt_dt
from agent.telegram.notifier import ConsoleNotifier

QUIZ_BANK_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "quiz_bank.json"
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


async def demo_notify(app: App) -> None:
    """M3 walkthrough on fixtures: an established bot sees Canvas changes → templated messages."""
    clock = app.clock
    assert isinstance(clock, FixedClock)
    svc = app.build_services(ConsoleNotifier())
    svc.llm = svc.agent = svc.batches = None  # offline; live mode summarises long announcements
    async with app.sessionmaker() as s:  # pretend earlier syncs already seeded these scopes
        for scope in ("assignments:101", "files:101", "announcements", "conversations", "missing"):
            s.add(SyncState(scope=scope, initialized=True))
        await s.commit()
    async with app.sessionmaker() as s:
        await sync_courses(s, app.canvas, clock)  # courses first, so the mapping can be confirmed
        for exam, sug in await suggestions_for_unconfirmed(s):
            await confirm_mapping(s, exam.id, sug.course_id)
        await s.commit()

    _section("1. Canvas sync → events → outbox → Telegram (13:00, outside quiet hours)")
    await app.sync_canvas()
    await jobs.tick(svc)

    _section("2. Next morning 08:30 — digest collects low-priority items (new files, new work)")
    _set_local(clock, 1, time(8, 30))
    await jobs.morning(svc)

    _section("3. /deadlines, /grades, /need networks (templates, no LLM)")
    async with app.sessionmaker() as s:
        _out(await deadlines_view(s, clock))
        _out("\n" + await grades_view(s, clock))
        exam = await s.scalar(select(Exam).where(Exam.title == "Computer Networks"))
        _out("\n" + render_need(exam, await need_for_exam(s, exam), ["A", "A-"]))


async def demo_study(app: App) -> None:
    """M5 walkthrough: course files → index → cited search → verification quiz → mock exam."""
    clock = app.clock
    assert isinstance(clock, FixedClock)
    svc = app.build_services(ConsoleNotifier())
    svc.llm = svc.agent = svc.batches = svc.quizzes = svc.voice = None  # offline
    svc.data_dir = Path(app.settings.data_dir) / "dry_run"
    app.settings = app.settings.model_copy(update={"data_dir": str(svc.data_dir)})

    async def ready(exam_id: int) -> None:
        return None

    svc.discovery = TopicDiscovery(app.sessionmaker, app.canvas, None, None, clock, ready)
    async with app.sessionmaker() as s:
        await sync_courses(s, app.canvas, clock)
        for exam, sug in await suggestions_for_unconfirmed(s):
            await confirm_mapping(s, exam.id, sug.course_id)
        await s.commit()
    await svc.discovery.start()
    async with app.sessionmaker() as s:
        for exam_id, level in DEMO_LEVELS.items():
            await ts.accept_topics(s, exam_id)
            await ts.rate_rest_of_exam(s, exam_id, level)
        await s.commit()

    _section("1. Canvas files (module items, Files tab hidden) → download → extract → FTS index")
    await app.sync_canvas()
    await app.index_files()
    async with app.sessionmaker() as s:
        for f in (await s.scalars(select(CourseFile).order_by(CourseFile.id))).all():
            n = await s.scalar(select(func.count()).select_from(Chunk).where(Chunk.file_id == f.id))
            _out(f"  {f.display_name:<38} {f.kind:<10} {f.index_state:<9} {n} chunks")

    _section("2. Agent tool search_course_files (no LLM): cited excerpts")
    from agent.brain.tool_handlers import ToolContext
    from agent.brain.tools import run_tool

    text, _ = await run_tool(
        ToolContext(app.sessionmaker, app.settings, clock),
        "material",
        "search_course_files",
        {"query": "fast retransmit triple duplicate ACK cwnd", "course": "Networks"},
    )
    _out(text)

    _section("3. Block ends → 3 quiz polls from the topic bank (filled overnight by a Batch)")
    bank = json.loads(await asyncio.to_thread(QUIZ_BANK_FIXTURE.read_text))
    async with app.sessionmaker() as s:
        topic = await s.scalar(
            select(Topic).where(Topic.name == "TCP reliability & congestion control")
        )
        await store_questions(s, topic.id, bank[topic.name], "batch", clock)
        v = PlanVersion(
            created_at=clock.now(),
            reason="demo",
            proposed_by="planner",
            applied_at=clock.now(),
            snapshot_json={"blocks": []},
        )
        s.add(v)
        await s.flush()
        s.add(
            Block(
                plan_version_id=v.id,
                starts_at=clock.now() - timedelta(minutes=50),
                ends_at=clock.now(),
                exam_id=topic.exam_id,
                topic_id=topic.id,
                kind="learn",
                status="started",
            )
        )
        await s.commit()
    await jobs.tick(svc)
    async with app.sessionmaker() as s:
        quiz = await s.scalar(select(Quiz).order_by(Quiz.id.desc()))
        outcome = None
        for i, q in enumerate(quiz.questions_json):  # student answers: 2 right, 1 wrong
            outcome = await record_answer(
                s, q["poll_id"], q["correct"] if i < 2 else (q["correct"] + 1) % 4, clock
            )
        await s.commit()
    from agent.telegram.quiz import outcome_text

    _out("\n── bot ──\n" + (outcome_text(outcome, outcome.purpose) or ""))

    _section("4. «начинаю past paper» → 120-min timer → score → weak topics")
    async with app.sessionmaker() as s:
        networks = await s.scalar(select(Exam).where(Exam.title == "Computer Networks"))
        paper = await s.scalar(select(CourseFile).where(CourseFile.kind == "past_paper"))
        m = await mock_mod.start_mock(s, networks.id, paper.id if paper else None, clock)
        await s.commit()
    _out(f"  mock #{m.id} started, ends {fmt_dt(m.ends_at, clock.zone)}")
    for minutes in (61, 50, 10):
        clock.advance(minutes=minutes)
        await jobs.tick(svc)
    async with app.sessionmaker() as s:
        m = await s.get(MockExam, m.id)
        await mock_mod.record_score(s, m, mock_mod.parse_score("36/50"))
        routing = await s.scalar(select(Topic).where(Topic.name == "Routing algorithms"))
        await mock_mod.toggle_topic(s, m.id, routing.id)
        _, weakened = await mock_mod.finish(s, m.id, clock)
        await s.commit()
    _out(
        f"\n── bot ──\nЗаписал mock: 72%. Слабые темы: {', '.join(weakened)} — ночью план добавит им блоков."
    )


COMMANDS = {
    "sync": demo_sync,
    "map": demo_mapping,
    "plan": demo_plan,
    "notify": demo_notify,
    "study": demo_study,
}


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
    if command in ("plan", "notify", "study"):
        if not dry_run:
            raise SystemExit(f"The {command} walkthrough only runs with --dry-run.")
        start = (
            datetime.combine(
                datetime.now(settings.zone).date() + timedelta(days=1), time(7, 0), settings.zone
            )
            if command == "plan"
            else datetime(2026, 11, 19, 13, 0, tzinfo=settings.zone)  # fixtures are dated around it
        )
        clock = FixedClock(start, settings.zone)
        settings = settings.model_copy(update={"anthropic_api_key": None, "log_level": "WARNING"})
    async with build_app(settings, dry_run=dry_run, clock=clock) as app:
        if command in ("plan", "notify", "study"):
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
