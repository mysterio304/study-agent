"""Callback data and keyboards. Callback payloads carry ids only; handlers re-load state from the
DB, so stale or replayed buttons are harmless."""

from __future__ import annotations

from typing import Any

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class PropCb(CallbackData, prefix="prop"):
    id: int
    opt: str  # A | B | KEEP


class BlockCb(CallbackData, prefix="blk"):
    id: int
    action: str  # start | move | done | fail


class TopicCb(CallbackData, prefix="tpc"):
    exam_id: int
    action: str  # accept | edit


class RateCb(CallbackData, prefix="rate"):
    topic_id: int
    level: int
    rest: bool = False


class DeadlineCb(CallbackData, prefix="dl"):
    """Packs as 'dl:<id>:<action>' — the format notify.templates.deadline_buttons builds."""

    id: int
    action: str  # done | snooze | ignore


class CheckinCb(CallbackData, prefix="chk"):
    action: str  # later


OPTION_LABELS = {"A": "✅ Вариант A", "B": "🅱️ Вариант B", "KEEP": "↩️ Оставить как есть"}


def proposal_kb(
    proposal_id: int, options: list[dict[str, Any]], *, single_label: str | None = None
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    keys = [o["key"] for o in options]
    for key in keys:
        label = OPTION_LABELS[key]
        if single_label and key == "A" and "B" not in keys:
            label = single_label
        kb.button(text=label, callback_data=PropCb(id=proposal_id, opt=key))
    kb.adjust(1)
    return kb.as_markup()


def block_start_kb(block_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Начал", callback_data=BlockCb(id=block_id, action="start"))
    kb.button(text="↪️ Перенести", callback_data=BlockCb(id=block_id, action="move"))
    return kb.as_markup()


def block_end_kb(block_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сделал", callback_data=BlockCb(id=block_id, action="done"))
    kb.button(text="✖️ Не вышло", callback_data=BlockCb(id=block_id, action="fail"))
    return kb.as_markup()


def topic_review_kb(exam_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Темы верные", callback_data=TopicCb(exam_id=exam_id, action="accept"))
    kb.button(text="✏️ Поправлю в чате", callback_data=TopicCb(exam_id=exam_id, action="edit"))
    kb.adjust(1)
    return kb.as_markup()


def rating_kb(topic_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for level, label in ((0, "0 не трогал"), (1, "1 читал"), (2, "2 решаю"), (3, "3 объясню")):
        kb.button(text=label, callback_data=RateCb(topic_id=topic_id, level=level))
    kb.button(
        text="Остальные темы курса = 1", callback_data=RateCb(topic_id=topic_id, level=1, rest=True)
    )
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def checkin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Отвечу позже", callback_data=CheckinCb(action="later"))
    return kb.as_markup()
