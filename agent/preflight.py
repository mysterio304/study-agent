"""Preflight: `python -m agent.preflight` — verify .env credentials with one read-only call per
service before starting the bot. Never prints secret values.

- Telegram: getMe (bot username)
- Canvas: GET /users/self and the active course list (read-only client)
- Anthropic: models.retrieve for both configured models (no tokens billed)
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable

import anthropic
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from agent.canvas.api import CanvasAPI
from agent.canvas.client import CanvasClient, CanvasError
from agent.config import ConfigError, Settings, get_settings


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


async def check_telegram(s: Settings) -> str:
    assert s.telegram_bot_token is not None
    bot = Bot(token=s.telegram_bot_token.get_secret_value())
    try:
        me = await bot.get_me()
    finally:
        await bot.session.close()
    return f"bot @{me.username}, owner id {s.telegram_owner_id}"


async def check_canvas(s: Settings) -> str:
    assert s.canvas_base_url and s.canvas_token
    async with CanvasClient(
        s.canvas_base_url, s.canvas_token.get_secret_value(), max_retries=1
    ) as client:
        await client.get_json("/api/v1/users/self")
        courses = await CanvasAPI(client).list_courses()
    names = "; ".join(c.name for c in courses[:8])
    return f"{len(courses)} active courses: {names}"


async def check_anthropic(s: Settings) -> str:
    assert s.anthropic_api_key is not None
    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())
    found = [(await client.models.retrieve(m)).id for m in (s.cheap_model, s.smart_model)]
    return "models available: " + ", ".join(found)


async def _run(name: str, fn: Callable[[Settings], Awaitable[str]], s: Settings) -> bool:
    try:
        _out(f"✅ {name}: {await fn(s)}")
        return True
    except (TelegramAPIError, CanvasError, anthropic.APIError) as exc:
        detail = getattr(exc, "status", None) or getattr(exc, "status_code", None) or ""
        _out(f"❌ {name}: {type(exc).__name__} {detail}".rstrip())
    except Exception as exc:  # network errors etc. — type only, never the message (may echo URLs)
        _out(f"❌ {name}: {type(exc).__name__}")
    return False


async def main() -> int:
    try:
        settings = get_settings()
        settings.require_live()
    except (ConfigError, ValueError) as exc:
        _out(f"❌ config: {exc}")
        return 1
    _out("✅ config: all required values present")
    results = [
        await _run("telegram", check_telegram, settings),
        await _run("canvas", check_canvas, settings),
        await _run("anthropic", check_anthropic, settings),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
