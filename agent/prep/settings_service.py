"""Runtime settings: DB overrides layered over .env defaults (ARCHITECTURE D7).

Editable keys can be changed through an agent proposal (you confirm with a button). Locked keys
are the planner's non-negotiables (SPEC §3.5) and can only change via an explicit /settings
command — never through chat.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import Settings
from agent.core.clock import Clock
from agent.core.timeutil import parse_time_range
from agent.db.models import Setting


class SettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    label_ru: str
    kind: type
    locked: bool = False
    min_value: float | None = None
    max_value: float | None = None


SPECS: dict[str, SettingSpec] = {
    s.key: s
    for s in (
        SettingSpec("cap_hours", "Лимит учёбы в день, ч", float, min_value=1, max_value=12),
        SettingSpec(
            "final_week_cap_hours", "Лимит в финальную неделю, ч", float, min_value=1, max_value=12
        ),
        SettingSpec(
            "between_exams_cap_hours", "Лимит между экзаменами, ч", float, min_value=1, max_value=12
        ),
        SettingSpec("study_window", "Окно учёбы (HH:MM-HH:MM)", str),
        SettingSpec("quiet_hours", "Тихие часы (HH:MM-HH:MM)", str),
        SettingSpec("lighter_weekday", "Лёгкий день (0=пн … 6=вс)", int, min_value=0, max_value=6),
        SettingSpec("voice_mode", "Голосовой режим (голос → голос)", bool),
        SettingSpec("morning_voice", "Утренний голосовой бриф", bool),
        # Non-negotiables: locked, shown in /settings, never changed by the agent.
        SettingSpec(
            "eve_review_only", "День перед экзаменом — только повторение", bool, locked=True
        ),
        SettingSpec("no_new_topics_eve", "Без новых тем накануне экзамена", bool, locked=True),
        SettingSpec(
            "protected_sleep", "Защищённый сон перед утренним экзаменом", bool, locked=True
        ),
        SettingSpec("no_blocks_during_exams", "Без блоков во время экзаменов", bool, locked=True),
    )
}


def defaults(settings: Settings) -> dict[str, Any]:
    return {
        "cap_hours": settings.daily_study_cap_hours,
        "final_week_cap_hours": settings.final_week_cap_hours,
        "between_exams_cap_hours": settings.between_exams_cap_hours,
        "study_window": settings.study_window,
        "quiet_hours": settings.quiet_hours,
        "lighter_weekday": 6,
        "voice_mode": False,
        "morning_voice": False,
        "eve_review_only": True,
        "no_new_topics_eve": True,
        "protected_sleep": True,
        "no_blocks_during_exams": True,
    }


def coerce(key: str, value: Any) -> Any:
    """Validate and normalise a value for `key`; raises SettingsError with a Russian message."""
    spec = SPECS.get(key)
    if spec is None:
        raise SettingsError(f"Неизвестная настройка: {key}")
    try:
        if spec.kind is bool:
            out: Any = (
                value
                if isinstance(value, bool)
                else str(value).lower() in ("1", "true", "on", "да")
            )
        elif spec.kind is str:
            out = str(value).strip()
            parse_time_range(out)
        else:
            out = spec.kind(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"Неверное значение для «{spec.label_ru}»: {value!r}") from exc
    if spec.min_value is not None and out < spec.min_value:
        raise SettingsError(f"«{spec.label_ru}»: минимум {spec.min_value}")
    if spec.max_value is not None and out > spec.max_value:
        raise SettingsError(f"«{spec.label_ru}»: максимум {spec.max_value}")
    return out


async def effective(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    values = defaults(settings)
    for row in (await session.scalars(select(Setting))).all():
        if row.key in SPECS:
            values[row.key] = row.value_json
    return values


async def set_value(
    session: AsyncSession,
    key: str,
    value: Any,
    clock: Clock,
    *,
    by: str,
    allow_locked: bool = False,
) -> Any:
    """Persist a setting. `allow_locked` is only ever passed by the /settings command handler."""
    spec = SPECS.get(key)
    if spec is None:
        raise SettingsError(f"Неизвестная настройка: {key}")
    if spec.locked and not allow_locked:
        raise SettingsError(f"«{spec.label_ru}» меняется только командой /settings")
    clean = coerce(key, value)
    row = await session.get(Setting, key) or Setting(key=key)
    row.value_json = clean
    row.is_locked = spec.locked
    row.updated_at = clock.now()
    row.updated_by = by
    session.add(row)
    await session.flush()
    return clean
