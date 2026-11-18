"""Audio format helpers. Telegram voice messages must be OGG/Opus; ffmpeg converts only when the
source isn't already OGG/Opus (SPEC §8: "convert with ffmpeg only if needed")."""

from __future__ import annotations

import asyncio
import shutil

FFMPEG_TIMEOUT_S = 60


class AudioError(RuntimeError):
    pass


def is_ogg_opus(data: bytes) -> bool:
    """OGG container ('OggS' capture pattern) carrying an Opus stream ('OpusHead')."""
    return data[:4] == b"OggS" and b"OpusHead" in data[:256]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def _ffmpeg(data: bytes, *out_args: str) -> bytes:
    if not ffmpeg_available():
        raise AudioError("ffmpeg is not installed (brew install ffmpeg / apt install ffmpeg)")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        *out_args,
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), FFMPEG_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()  # reap the process (no zombie)
        raise AudioError("ffmpeg timed out") from None
    if proc.returncode != 0 or not out:
        raise AudioError(f"ffmpeg failed: {err.decode(errors='ignore')[:200]}")
    return out


async def to_ogg_opus(data: bytes) -> bytes:
    """Any audio → OGG/Opus mono 48 kHz (what Telegram's sendVoice expects)."""
    if is_ogg_opus(data):
        return data
    return await _ffmpeg(
        data, "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "48k", "-f", "ogg"
    )


async def to_mp3(data: bytes) -> bytes:
    """Fallback for STT if the service rejects the original container."""
    return await _ffmpeg(data, "-ac", "1", "-f", "mp3")
