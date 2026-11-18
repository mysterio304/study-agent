"""Voice input (SPEC §8): Telegram voice (OGG/Opus) → ElevenLabs STT → normal agent turn.
Also `/voice on|off` (voice mode: voice in → voice out) and `/voice brief on|off` (morning brief)."""

from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from agent.core.text import shorten
from agent.flows import Services
from agent.prep.settings_service import set_value
from agent.telegram.chat import handle_user_text
from agent.telegram.format import h
from agent.voice.audio import AudioError
from agent.voice.elevenlabs import VoiceError

log = logging.getLogger(__name__)
router = Router(name="voice")

MAX_VOICE_SECONDS = 300  # longer notes: ask to split (STT is billed per minute)
TRANSCRIPT_ECHO_CHARS = 300


@router.message(F.voice)
async def on_voice(message: Message, svc: Services) -> None:
    if svc.voice is None:
        await message.answer("Голосовой ввод не настроен (нет ELEVENLABS_API_KEY). Напиши текстом.")
        return
    voice = message.voice
    assert voice is not None
    if voice.duration and voice.duration > MAX_VOICE_SECONDS:
        await message.answer("Это слишком длинное голосовое — до 5 минут, пожалуйста.")
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    buf = io.BytesIO()
    try:
        await message.bot.download(voice, destination=buf)
        text = await svc.voice.transcribe(buf.getvalue())
    except (VoiceError, AudioError, TelegramAPIError) as exc:
        log.warning("Voice transcription failed: %s", exc)
        await message.answer(
            "Не получилось распознать голосовое. Попробуй ещё раз или напиши текстом."
        )
        return
    if not text.strip():
        await message.answer("Не расслышал ни слова — попробуй ещё раз.")
        return
    await message.answer(f"🎙 <i>{h(shorten(text, TRANSCRIPT_ECHO_CHARS))}</i>")
    await handle_user_text(message, svc, text, via="voice")


@router.message(Command("voice"))
async def cmd_voice(message: Message, command: CommandObject, svc: Services) -> None:
    args = (command.args or "").lower().split()
    key = "morning_voice" if args[:1] == ["brief"] else "voice_mode"
    value = args[-1] if args else ""
    if value not in ("on", "off"):
        await message.answer(
            "Формат: <code>/voice on|off</code> или <code>/voice brief on|off</code>"
        )
        return
    async with svc.sessionmaker() as s:
        await set_value(s, key, value == "on", svc.clock, by="user")
        await s.commit()
    label = "Утренний голосовой бриф" if key == "morning_voice" else "Голосовой режим"
    await message.answer(f"{label}: {'включён' if value == 'on' else 'выключен'}.")
