"""Bot/dispatcher construction. Dependencies are injected into handlers by name via
dispatcher workflow data (`sessionmaker`, `settings`, ...)."""

from __future__ import annotations

from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from agent.config import Settings
from agent.telegram import setup_wizard
from agent.telegram.middleware import OwnerOnlyMiddleware


def build_bot(settings: Settings) -> Bot:
    assert settings.telegram_bot_token is not None
    return Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher(settings: Settings, **deps: Any) -> Dispatcher:
    assert settings.telegram_owner_id is not None
    dp = Dispatcher(settings=settings, **deps)
    dp.update.outer_middleware(OwnerOnlyMiddleware(settings.telegram_owner_id))
    dp.include_router(setup_wizard.router)
    return dp
