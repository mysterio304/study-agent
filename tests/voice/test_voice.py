"""ElevenLabs client (HTTP mocked), audio helpers, voice service, reply decision, morning brief."""

from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import func, select

from agent.core.clock import FixedClock
from agent.db.models import LlmUsage, SttTranscript
from agent.voice import audio as audio_mod
from agent.voice.audio import AudioError, is_ogg_opus, to_ogg_opus
from agent.voice.elevenlabs import BASE_URL, ElevenLabs, VoiceError
from agent.voice.service import FALLBACK_VOICE_ID, VoiceService, gist, wants_voice
from tests.planner.conftest import HKT, at

KEY = "el-test-key-not-real-123"
OGG = b"OggS\x00\x02" + b"\x00" * 22 + b"OpusHead" + b"\x00" * 40


def client(**kw) -> ElevenLabs:
    return ElevenLabs(
        KEY,
        stt_model="scribe_v2",
        tts_model=kw.get("tts", "eleven_flash_v2_5"),
        voice_id=kw.get("voice", "VOICE1"),
    )


@respx.mock
async def test_transcribe_sends_multipart_with_model_and_key() -> None:
    route = respx.post(f"{BASE_URL}/v1/speech-to-text").mock(
        return_value=httpx.Response(
            200,
            json={"text": " Сделал два блока ", "language_code": "rus", "audio_duration_secs": 3.2},
        )
    )
    el = client()
    t = await el.transcribe(OGG)
    await el.aclose()
    assert t.text == "Сделал два блока" and t.duration_s == 3.2
    req = route.calls.last.request
    assert req.headers["xi-api-key"] == KEY
    assert b'name="model_id"' in req.content and b"scribe_v2" in req.content


@respx.mock
async def test_synthesize_uses_voice_path_and_opus_format() -> None:
    route = respx.post(f"{BASE_URL}/v1/text-to-speech/VOICE1").mock(
        return_value=httpx.Response(200, content=OGG)
    )
    el = client()
    assert await el.synthesize("Привет") == OGG
    await el.aclose()
    assert route.calls.last.request.url.params["output_format"] == "opus_48000_64"


@respx.mock
async def test_errors_carry_status_but_never_the_key() -> None:
    respx.post(f"{BASE_URL}/v1/text-to-speech/VOICE1").mock(
        return_value=httpx.Response(402, json={})
    )
    el = client()
    with pytest.raises(VoiceError) as exc:
        await el.synthesize("x")
    await el.aclose()
    assert exc.value.status == 402 and KEY not in str(exc.value)
    assert KEY not in repr(el)


@respx.mock
async def test_describe_handles_scoped_keys() -> None:
    respx.get(f"{BASE_URL}/v1/models").mock(
        return_value=httpx.Response(401, json={"detail": {"status": "missing_permissions"}})
    )
    el = client()
    assert "scoped" in await el.describe()
    await el.aclose()


def test_ogg_detection_and_passthrough() -> None:
    assert is_ogg_opus(OGG) and not is_ogg_opus(b"ID3\x03mp3data")


async def test_conversion_without_ffmpeg_fails_clearly(monkeypatch) -> None:
    assert await to_ogg_opus(OGG) == OGG  # no ffmpeg needed for OGG/Opus
    monkeypatch.setattr(audio_mod.shutil, "which", lambda _: None)
    with pytest.raises(AudioError, match="ffmpeg"):
        await to_ogg_opus(b"ID3 mp3")


def test_gist_and_voice_request_detection() -> None:
    long = (
        "<b>План</b> на сегодня готов. Первый блок в 09:00. Потом Routing.\n"
        "• 09:00 TCP\n• 10:00 DP\nЕщё одно длинное предложение " + "x" * 400
    )
    g = gist(long)
    assert (
        len(g) <= 300 and "•" not in g and "<b>" not in g and g.startswith("План на сегодня готов.")
    )
    assert wants_voice("ответь голосом, пожалуйста") and not wants_voice("ответь текстом")


class FakeEl:
    def __init__(self, fail_voice: bool = False) -> None:
        self.calls, self.voice, self.fail_voice = 0, "LIB", fail_voice
        self.can_speak = True

    async def transcribe(self, audio, **kw):
        self.calls += 1
        return SimpleNamespace(text="Сделал TCP", language="rus", duration_s=2.0)

    async def synthesize(self, text):
        if self.fail_voice and self.voice == "LIB":
            raise VoiceError("paid", 402)
        return OGG

    def use_voice(self, voice_id):
        self.voice = voice_id


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(at(date(2026, 11, 19), 12), HKT)


async def test_transcripts_are_cached(sessionmaker, clk) -> None:
    el = FakeEl()
    svc = VoiceService(el, sessionmaker, clk, tts_daily_char_limit=1000)
    assert await svc.transcribe(OGG) == "Сделал TCP"
    assert await svc.transcribe(OGG) == "Сделал TCP"
    assert el.calls == 1
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(SttTranscript)) == 1


async def test_daily_tts_cap_and_402_fallback(sessionmaker, clk) -> None:
    el = FakeEl(fail_voice=True)
    svc = VoiceService(el, sessionmaker, clk, tts_daily_char_limit=40)
    audio, notice = await svc.speak("Короткий ответ.")
    assert audio == OGG and el.voice == FALLBACK_VOICE_ID and "платный" in notice
    audio, notice = await svc.speak("Ещё один ответ, довольно длинный.")
    assert audio is None and "лимит" in notice
    async with sessionmaker() as s:
        assert (
            await s.scalar(
                select(func.sum(LlmUsage.output_tokens)).where(LlmUsage.purpose == "tts")
            )
            == 15
        )


async def test_reply_is_spoken_only_when_asked_or_in_voice_mode(sessionmaker, clk) -> None:
    from agent.canvas.fake import FakeCanvasSource
    from agent.config import Settings
    from agent.flows import Services
    from agent.prep.settings_service import set_value
    from agent.telegram.chat import _maybe_speak
    from tests.notify.test_jobs import Recorder

    sent: list[str] = []

    async def answer_voice(file):
        sent.append("voice")

    async def answer(text, **kw):
        sent.append("text")

    msg = SimpleNamespace(answer_voice=answer_voice, answer=answer)
    svc = Services(Settings(_env_file=None), sessionmaker, clk, FakeCanvasSource(), Recorder())
    svc.voice = VoiceService(FakeEl(), sessionmaker, clk, tts_daily_char_limit=1000)
    await _maybe_speak(msg, svc, "привет", None, "Ответ.", via="text")
    assert sent == []
    await _maybe_speak(msg, svc, "ответь голосом", None, "Ответ.", via="text")
    assert sent == ["voice"]
    await _maybe_speak(msg, svc, "что сегодня?", None, "Ответ.", via="voice")  # voice mode off
    assert sent == ["voice"]
    async with sessionmaker() as s:
        await set_value(s, "voice_mode", True, clk, by="user")
        await s.commit()
    await _maybe_speak(msg, svc, "что сегодня?", None, "Ответ.", via="voice")
    await _maybe_speak(msg, svc, "что сегодня?", "Суть.", "Ответ.", via="text")  # agent tool
    assert sent == ["voice", "voice", "voice"]


async def test_morning_voice_brief(sessionmaker, clk) -> None:
    from agent.canvas.fake import FakeCanvasSource
    from agent.config import Settings
    from agent.db.models import Topic
    from agent.flows import Services, apply_choice, propose_replan
    from agent.notify import jobs
    from agent.prep.seed import seed_exams
    from agent.prep.settings_service import set_value
    from tests.notify.test_jobs import Recorder

    c = FixedClock(at(date(2026, 11, 20), 7), HKT)
    async with sessionmaker() as s:
        await seed_exams(s)
        await s.flush()
        s.add(
            Topic(
                exam_id=1,
                name="TCP",
                effort_hours=3,
                importance=3,
                level=1,
                target_level=2,
                status="active",
            )
        )
        await set_value(s, "morning_voice", True, c, by="user")
        await s.commit()
    svc = Services(Settings(_env_file=None), sessionmaker, c, FakeCanvasSource(), Recorder())
    svc.voice = VoiceService(FakeEl(), sessionmaker, c, tts_daily_char_limit=1000)
    await apply_choice(svc, await propose_replan(svc, "x", send=False), "A")
    c.advance(hours=1, minutes=30)
    await jobs.morning(svc)
    assert svc.notifier.texts()[-1].startswith("[voice]")


async def test_concurrent_duplicate_voice_notes_transcribe_once(sessionmaker, clk) -> None:
    import asyncio

    el = FakeEl()
    svc = VoiceService(el, sessionmaker, clk, tts_daily_char_limit=1000)
    texts = await asyncio.gather(svc.transcribe(OGG), svc.transcribe(OGG))
    assert texts == ["Сделал TCP", "Сделал TCP"] and el.calls == 1


async def test_voice_failure_never_loses_the_text_answer(sessionmaker, clk) -> None:
    from agent.canvas.fake import FakeCanvasSource
    from agent.config import Settings
    from agent.flows import Services
    from agent.telegram.chat import _maybe_speak
    from tests.notify.test_jobs import Recorder

    async def broken_voice(file):
        raise RuntimeError("telegram network down")

    async def answer(text, **kw):
        return None

    msg = SimpleNamespace(answer_voice=broken_voice, answer=answer)
    svc = Services(Settings(_env_file=None), sessionmaker, clk, FakeCanvasSource(), Recorder())
    svc.voice = VoiceService(FakeEl(), sessionmaker, clk, tts_daily_char_limit=1000)
    await _maybe_speak(msg, svc, "ответь голосом", None, "Ответ.", via="text")  # must not raise


@respx.mock
async def test_non_json_stt_response_is_a_voice_error() -> None:
    respx.post(f"{BASE_URL}/v1/speech-to-text").mock(
        return_value=httpx.Response(200, text="<html>oops")
    )
    el = client()
    with pytest.raises(VoiceError):
        await el.transcribe(OGG)
    await el.aclose()
