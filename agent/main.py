"""Entrypoint: `python -m agent [--dry-run]`.

Live mode needs Telegram/Canvas/Anthropic credentials in .env. Dry-run uses fixture Canvas
data and a separate database; if a Telegram token is configured the bot still runs, otherwise
a console demo is printed instead.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agent.app import App, build_app
from agent.config import ConfigError, get_settings
from agent.telegram.bot import build_bot, build_dispatcher

log = logging.getLogger("agent")


def _schedule_jobs(app: App) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=app.settings.zone)
    scheduler.add_job(
        app.sync_canvas,
        "interval",
        minutes=app.settings.canvas_poll_minutes,
        id="canvas_sync",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    return scheduler


async def _run_bot(app: App) -> None:
    bot = build_bot(app.settings)
    dp = build_dispatcher(app.settings, sessionmaker=app.sessionmaker, clock=app.clock)
    scheduler = _schedule_jobs(app)
    await app.sync_canvas()  # first sync seeds state before the bot starts answering
    scheduler.start()
    try:
        log.info("Bot started (dry_run=%s)", app.dry_run)
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
            from agent.cli import demo_mapping

            log.warning("No Telegram credentials; running console demo instead of the bot")
            await app.sync_canvas()
            await demo_mapping(app)


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
