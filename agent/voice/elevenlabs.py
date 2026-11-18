"""Minimal async ElevenLabs client: speech-to-text and text-to-speech (docs checked Nov 2026).

STT: POST /v1/speech-to-text (multipart: model_id, file, language_code) → {"text", ...}
TTS: POST /v1/text-to-speech/{voice_id}?output_format=… (JSON: text, model_id) → audio bytes
Auth: `xi-api-key` header. Errors never include the key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.elevenlabs.io"
TTS_FORMAT = "opus_48000_64"  # Opus; container verified at runtime (see agent.voice.audio)


class VoiceError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str | None
    duration_s: float | None


class ElevenLabs:
    def __init__(
        self,
        api_key: str,
        *,
        stt_model: str,
        tts_model: str | None,
        voice_id: str | None,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._stt_model, self._tts_model, self._voice_id = stt_model, tts_model, voice_id
        self._http = httpx.AsyncClient(
            base_url=BASE_URL, headers={"xi-api-key": api_key}, timeout=timeout, transport=transport
        )

    def __repr__(self) -> str:
        return f"ElevenLabs(stt={self._stt_model!r}, tts={self._tts_model!r})"

    def use_voice(self, voice_id: str) -> None:
        self._voice_id = voice_id

    @property
    def can_speak(self) -> bool:
        return bool(self._tts_model and self._voice_id)

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _check(response: httpx.Response, what: str) -> None:
        if response.status_code >= 400:
            raise VoiceError(
                f"ElevenLabs {what} failed ({response.status_code})", response.status_code
            )

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "voice.ogg",
        mime: str = "audio/ogg",
        language: str | None = None,
    ) -> Transcript:
        data = {"model_id": self._stt_model, "tag_audio_events": "false"}
        if language:
            data["language_code"] = language
        try:
            r = await self._http.post(
                "/v1/speech-to-text", data=data, files={"file": (filename, audio, mime)}
            )
        except httpx.TransportError as exc:
            raise VoiceError(f"ElevenLabs STT network error: {type(exc).__name__}") from None
        self._check(r, "speech-to-text")
        try:
            body: dict[str, Any] = r.json()
        except ValueError:
            raise VoiceError("ElevenLabs speech-to-text returned a non-JSON response") from None
        return Transcript(
            text=str(body.get("text") or "").strip(),
            language=body.get("language_code"),
            duration_s=body.get("audio_duration_secs"),
        )

    async def synthesize(self, text: str) -> bytes:
        if not self.can_speak:
            raise VoiceError("TTS is not configured (ELEVENLABS_TTS_MODEL / ELEVENLABS_VOICE_ID)")
        try:
            r = await self._http.post(
                f"/v1/text-to-speech/{self._voice_id}",
                params={"output_format": TTS_FORMAT},
                json={"text": text, "model_id": self._tts_model},
            )
        except httpx.TransportError as exc:
            raise VoiceError(f"ElevenLabs TTS network error: {type(exc).__name__}") from None
        self._check(r, "text-to-speech")
        return r.content

    async def describe(self) -> str:
        """Preflight: verify the key, the TTS model and the voice (no audio is generated)."""
        r = await self._http.get("/v1/models")
        if r.status_code == 401 and "missing_permissions" in r.text:
            return (
                f"key valid but scoped (no models/voices read permission); "
                f"stt {self._stt_model}, tts {self._tts_model or '—'} not verified"
            )
        self._check(r, "models")
        models = {m.get("model_id"): m for m in r.json()}
        parts = []
        if self._tts_model:
            m = models.get(self._tts_model)
            if m is None or not m.get("can_do_text_to_speech"):
                raise VoiceError(f"TTS model {self._tts_model!r} not available")
            langs = {lang.get("language_id") for lang in m.get("languages") or []}
            parts.append(f"tts {self._tts_model} ({'ru ok' if 'ru' in langs else 'ru NOT listed'})")
        if self._voice_id:
            v = await self._http.get(f"/v1/voices/{self._voice_id}")
            if v.status_code < 400:
                parts.append(f"voice «{v.json().get('name', '?')}»")
            else:
                parts.append(f"voice not verified ({v.status_code})")
        parts.append(f"stt {self._stt_model}")
        return ", ".join(parts)
