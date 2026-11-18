"""Voice features (SPEC §8): cached speech-to-text and short text-to-speech replies.

Rules:
- voice in → transcript (cached by audio SHA-256, never transcribed twice) → normal agent turn;
- voice out only on request («ответь голосом» / the agent's reply_with_voice tool), in voice mode
  (voice in → voice out), or for the optional morning brief;
- voice = short gist (≤ GIST_MAX_CHARS), text = details: schedules/lists are always sent as text;
- TTS characters per day are capped (TTS is billed per character);
- if the configured voice needs a paid plan (HTTP 402), fall back to a premade voice once.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.brain.accounting import local_day_start
from agent.core.clock import Clock
from agent.core.text import shorten
from agent.db.models import LlmUsage, SttTranscript
from agent.voice.audio import AudioError, to_mp3, to_ogg_opus
from agent.voice.elevenlabs import ElevenLabs, VoiceError

log = logging.getLogger(__name__)

GIST_MAX_CHARS = 300
FALLBACK_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # ElevenLabs premade voice (usable on the free plan)
FALLBACK_RETRY = timedelta(hours=1)  # after a 402, retry the configured voice this often
VOICE_REQUEST_RE = re.compile(
    r"(голосом|голосовым|озвучь|скажи вслух|voice reply|reply with voice)", re.I
)
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


def wants_voice(text: str) -> bool:
    return bool(VOICE_REQUEST_RE.search(text))


def gist(text: str, limit: int = GIST_MAX_CHARS) -> str:
    """Speakable short version: no HTML, no list lines, first sentences up to `limit` chars."""
    plain = re.sub(r"<[^>]+>", "", text)
    lines = [
        ln.strip()
        for ln in plain.splitlines()
        if ln.strip() and not re.match(r"^([•\-*]|\d+[.)])\s", ln.strip())
    ]
    out = ""
    for sentence in _SENTENCE_RE.split(" ".join(lines)):
        if len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    return out or shorten(" ".join(lines) or plain, limit)


class VoiceService:
    def __init__(
        self,
        client: ElevenLabs,
        sessionmaker: async_sessionmaker,
        clock: Clock,
        *,
        tts_daily_char_limit: int,
    ) -> None:
        self._el, self._sm, self._clock, self._limit = (
            client,
            sessionmaker,
            clock,
            tts_daily_char_limit,
        )
        self._preferred_voice = client.voice_id  # what ELEVENLABS_VOICE_ID asks for
        self._fallback_until: datetime | None = None  # temporary: a plan upgrade is picked up
        self._locks: dict[str, asyncio.Lock] = {}

    async def transcribe(self, audio: bytes) -> str:
        digest = hashlib.sha256(audio).hexdigest()
        async with self._locks.setdefault(
            digest, asyncio.Lock()
        ):  # same audio twice → one STT call
            try:
                return await self._transcribe_once(audio, digest)
            finally:
                self._locks.pop(digest, None)

    async def _transcribe_once(self, audio: bytes, digest: str) -> str:
        async with self._sm() as s:
            cached = await s.get(SttTranscript, digest)
        if cached is not None:
            return cached.text
        try:
            result = await self._el.transcribe(audio)
        except VoiceError as exc:
            if exc.status not in (400, 415, 422):
                raise
            result = await self._el.transcribe(
                await to_mp3(audio), filename="voice.mp3", mime="audio/mpeg"
            )
        async with self._sm() as s:
            await s.merge(  # idempotent even if another process cached it meanwhile
                SttTranscript(
                    audio_sha256=digest,
                    text=result.text,
                    model="elevenlabs-stt",
                    created_at=self._clock.now(),
                )
            )
            s.add(
                LlmUsage(
                    at=self._clock.now(),
                    model="elevenlabs",
                    purpose="stt",
                    input_tokens=int(result.duration_s or 0),
                    cost_usd=0.0,
                )
            )
            await s.commit()
        return result.text

    async def chars_today(self) -> int:
        async with self._sm() as s:
            total = await s.scalar(
                select(func.coalesce(func.sum(LlmUsage.output_tokens), 0)).where(
                    LlmUsage.purpose == "tts", LlmUsage.at >= local_day_start(self._clock)
                )
            )
        return int(total or 0)

    async def speak(self, text: str) -> tuple[bytes | None, str | None]:
        """(OGG/Opus audio, notice). Audio is None when TTS is unavailable or over the daily cap;
        `notice` is a one-off message for the student (e.g. the voice fallback)."""
        if not self._el.can_speak:
            return None, None
        short = gist(text)
        if not short:
            return None, None
        if await self.chars_today() + len(short) > self._limit:
            return None, "Дневной лимит голосовых ответов исчерпан — отвечаю текстом."
        notice = None
        now = self._clock.now()
        in_fallback = self._fallback_until is not None and now < self._fallback_until
        self._el.use_voice(FALLBACK_VOICE_ID if in_fallback else self._preferred_voice)
        try:
            audio = await self._el.synthesize(short)
        except VoiceError as exc:
            if exc.status != 402 or in_fallback:
                log.warning("TTS failed: %s", exc)
                return None, None
            # The chosen voice needs a paid plan: use a premade voice for a while, then retry it.
            self._fallback_until = now + FALLBACK_RETRY
            self._el.use_voice(FALLBACK_VOICE_ID)
            notice = (
                "Выбранный голос ElevenLabs сейчас недоступен по тарифу — временно отвечаю "
                "стандартным голосом и попробую снова через час."
            )
            audio = await self._el.synthesize(short)
        try:
            audio = await to_ogg_opus(audio)
        except AudioError as exc:
            log.warning("Cannot convert TTS audio: %s", exc)
            return None, notice
        async with self._sm() as s:
            s.add(
                LlmUsage(
                    at=self._clock.now(),
                    model="elevenlabs",
                    purpose="tts",
                    output_tokens=len(short),
                    cost_usd=0.0,
                )
            )
            await s.commit()
        return audio, notice
