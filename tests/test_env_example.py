"""Guard: .env.example is committed, so it must never contain real secret values."""

from pathlib import Path

SECRET_KEYS = {
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_ID",
    "CANVAS_TOKEN",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "ANTHROPIC_API_KEY",
}


def test_env_example_has_no_secret_values() -> None:
    lines = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    filled = sorted(k for k in SECRET_KEYS if values.get(k, "").split("#")[0].strip())
    assert filled == [], f"Secret values committed in .env.example: {filled} — move them to .env"
