"""Bot/dispatcher construction. The `Services` container is injected into handlers by name."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from agent.config import Settings
from agent.flows import Services
from agent.telegram import chat, commands, files, mock, quiz, setup_wizard, voice
from agent.telegram.middleware import OwnerOnlyMiddleware

COMMANDS = [
    BotCommand(command="today", description="План на сегодня"),
    BotCommand(command="plan", description="План на 3 дня"),
    BotCommand(command="progress", description="Прогресс по темам"),
    BotCommand(command="deadlines", description="Дедлайны на 2 недели"),
    BotCommand(command="grades", description="Оценки по курсам"),
    BotCommand(command="need", description="Сколько нужно на final: /need networks A-"),
    BotCommand(command="usage", description="Расход на LLM"),
    BotCommand(command="undo", description="Вернуть прошлую версию плана"),
    BotCommand(command="replan", description="Пересчитать план"),
    BotCommand(command="checkin", description="Вечерний check-in"),
    BotCommand(command="mock", description="Mock exam с таймером"),
    BotCommand(command="diagnostic", description="Диагностика курса: /diagnostic networks"),
    BotCommand(command="voice", description="Голос: /voice on|off, /voice brief on|off"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="start", description="Мастер настройки"),
]


def build_bot(settings: Settings) -> Bot:
    assert settings.telegram_bot_token is not None
    return Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher(settings: Settings, svc: Services) -> Dispatcher:
    assert settings.telegram_owner_id is not None
    dp = Dispatcher(svc=svc)
    dp.update.outer_middleware(OwnerOnlyMiddleware(settings.telegram_owner_id))
    dp.include_routers(
        setup_wizard.router,
        commands.router,
        quiz.router,
        mock.router,
        voice.router,
        files.router,
        chat.router,
    )
    return dp
