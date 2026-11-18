"""Entrypoint: `python -m agent [--dry-run]`.

Live mode needs Telegram/Canvas/Anthropic credentials in .env. Dry-run uses fixture Canvas data
and a separate database; if a Telegram token is configured the bot still runs, otherwise a
console demo is printed instead.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agent.app import App, build_app
from agent.config import ConfigError, get_settings
from agent.flows import Services
from agent.notify import jobs
from agent.telegram.bot import COMMANDS, build_bot, build_dispatcher
from agent.telegram.notifier import TelegramNotifier

log = logging.getLogger("agent")
NIGHTLY_REPLAN = (3, 30)
WEEKLY_REPORT = ("sun", 20)


def _guard(name: str, job: Callable[[], Awaitable[None]]) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        try:
            await job()
        except Exception:
            log.exception("Job %s failed", name)

    return run


def _schedule_jobs(app: App, svc: Services) -> AsyncIOScheduler:
    s = app.settings
    scheduler = AsyncIOScheduler(timezone=s.zone)
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
    scheduler.add_job(
        _guard("canvas_sync", app.sync_canvas),
        "interval",
        minutes=s.canvas_poll_minutes,
        id="canvas_sync",
        **common,
    )
    scheduler.add_job(
        _guard("tick", lambda: jobs.tick(svc)), "interval", minutes=1, id="tick", **common
    )
    scheduler.add_job(
        _guard("morning", lambda: jobs.morning(svc)),
        "cron",
        hour=s.morning_time.hour,
        minute=s.morning_time.minute,
        id="morning",
        **common,
    )
    scheduler.add_job(
        _guard("evening", lambda: jobs.evening(svc)),
        "cron",
        hour=s.evening_time.hour,
        minute=s.evening_time.minute,
        id="evening",
        **common,
    )
    scheduler.add_job(
        _guard("weekly", lambda: jobs.weekly(svc)),
        "cron",
        day_of_week=WEEKLY_REPORT[0],
        hour=WEEKLY_REPORT[1],
        minute=0,
        id="weekly",
        **common,
    )
    scheduler.add_job(
        _guard("nightly", lambda: jobs.nightly(svc)),
        "cron",
        hour=NIGHTLY_REPLAN[0],
        minute=NIGHTLY_REPLAN[1],
        id="nightly",
        **common,
    )
    return scheduler


async def _run_bot(app: App) -> None:
    bot = build_bot(app.settings)
    assert app.settings.telegram_owner_id is not None
    svc = app.build_services(TelegramNotifier(bot, app.settings.telegram_owner_id))
    dp = build_dispatcher(app.settings, svc)
    scheduler = _schedule_jobs(app, svc)
    await app.sync_canvas()  # first sync seeds state before the bot starts answering
    scheduler.start()
    try:
        await bot.set_my_commands(COMMANDS)
        log.info("Bot started (dry_run=%s, llm=%s)", app.dry_run, svc.llm is not None)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


async def _amain(dry_run: bool) -> None:
    settings = get_settings()
    if not dry_run:
        settings.require_live()
    async with build_app(settings, dry_run=dry_run) as app:
        if settings.telegram_bot_token and settings.telegram_owner_id:
            await _run_bot(app)
        else:
            from agent.cli import demo_plan

            log.warning("No Telegram credentials; running console demo instead of the bot")
            await demo_plan(app)


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent", description="Personal study agent")
    parser.add_argument("--dry-run", action="store_true", help="use fixture data, not real APIs")
    args = parser.parse_args()
    try:
        asyncio.run(_amain(args.dry_run))
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
