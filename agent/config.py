"""Application configuration loaded from environment / `.env`.

Secrets are typed as `SecretStr` so they never appear in reprs or logs. In dry-run mode
the external credentials are optional; `Settings.require_live()` enforces them otherwise.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent.core.timeutil import TimeRange, parse_hhmm, parse_time_range


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---------------------------------------------------------------------------
    telegram_bot_token: SecretStr | None = None
    telegram_owner_id: int | None = None

    # --- Canvas (read-only) -------------------------------------------------------------------
    canvas_base_url: str | None = None
    canvas_token: SecretStr | None = None
    canvas_poll_minutes: int = Field(default=12, ge=5, le=60)

    # --- ElevenLabs ---------------------------------------------------------------------------
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_stt_model: str = "scribe_v2"
    elevenlabs_tts_model: str | None = None
    elevenlabs_voice_id: str | None = None

    # --- Anthropic ----------------------------------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    agent_model: str = "claude-sonnet-5-5"
    cheap_model: str = "claude-haiku-5-5"

    # --- Behaviour ----------------------------------------------------------------------------
    tz: str = "Asia/Hong_Kong"
    daily_study_cap_hours: float = Field(default=6.5, gt=0, le=14)
    final_week_cap_hours: float = Field(default=8.5, gt=0, le=14)
    between_exams_cap_hours: float = Field(default=7.5, gt=0, le=14)
    study_window: str = "09:00-23:00"
    quiet_hours: str = "00:30-08:00"
    morning_digest_time: str = "08:30"
    evening_checkin_time: str = "21:30"
    autonomy_mode: Literal["small_auto", "confirm_all"] = "small_auto"
    max_agent_steps: int = Field(default=8, ge=1, le=30)
    daily_llm_budget_usd: float = Field(default=2.0, ge=0)

    # --- Storage ------------------------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///data/agent.db"
    data_dir: str = "data"
    log_level: str = "INFO"

    @field_validator("canvas_base_url")
    @classmethod
    def _strip_slash(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().rstrip("/")
        if not v.startswith("https://"):
            raise ValueError("CANVAS_BASE_URL must start with https://")
        return v

    @field_validator("study_window", "quiet_hours")
    @classmethod
    def _valid_range(cls, v: str) -> str:
        parse_time_range(v)
        return v

    @field_validator("morning_digest_time", "evening_checkin_time")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        parse_hhmm(v)
        return v

    @field_validator("tz")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        ZoneInfo(v)  # raises if unknown
        return v

    # --- Derived helpers ---------------------------------------------------------------------
    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def study_window_range(self) -> TimeRange:
        return parse_time_range(self.study_window)

    @property
    def quiet_hours_range(self) -> TimeRange:
        return parse_time_range(self.quiet_hours)

    @property
    def morning_time(self) -> time:
        return parse_hhmm(self.morning_digest_time)

    @property
    def evening_time(self) -> time:
        return parse_hhmm(self.evening_checkin_time)

    def require_live(self) -> None:
        """Fail fast (without revealing values) if live-mode credentials are missing."""
        required = {
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_OWNER_ID": self.telegram_owner_id,
            "CANVAS_BASE_URL": self.canvas_base_url,
            "CANVAS_TOKEN": self.canvas_token,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError(
                "Missing required settings: " + ", ".join(missing) + ". See .env.example."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
