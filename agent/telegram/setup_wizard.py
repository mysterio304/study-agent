"""/start wizard, step 1: confirm which Canvas course belongs to each final.

Later milestones add the remaining steps (topic review, self-rating, settings).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent.config import Settings
from agent.prep.mapping_service import confirm_mapping, suggestions_for_unconfirmed
from agent.telegram.format import fmt_dt, h

router = Router(name="setup_wizard")

NO_COURSE = 0


class MapCb(CallbackData, prefix="map"):
    exam_id: int
    course_id: int  # NO_COURSE == "no Canvas course"


async def _next_question(
    sessionmaker: async_sessionmaker, settings: Settings
) -> tuple[str, InlineKeyboardMarkup | None]:
    async with sessionmaker() as session:
        pending = await suggestions_for_unconfirmed(session)
    if not pending:
        return (
            "✅ Все экзамены привязаны к курсам Canvas.\n"
            "Следующий шаг мастера — список тем — появится в следующем обновлении.",
            None,
        )
    exam, s = pending[0]
    kb = InlineKeyboardBuilder()
    lines = [
        f"<b>Экзамен:</b> {h(exam.title)} — {fmt_dt(exam.starts_at, settings.zone)}",
    ]
    if s.course_id is not None:
        lines.append(f"Похоже, это курс <b>{h(s.course_name)}</b>. Верно?")
        kb.button(text="✅ Да", callback_data=MapCb(exam_id=exam.id, course_id=s.course_id))
    else:
        lines.append("Не нашёл подходящий курс. Выбери из списка:")
    for cid, name, _score in s.alternatives:
        if cid != s.course_id:
            kb.button(text=name[:60], callback_data=MapCb(exam_id=exam.id, course_id=cid))
    kb.button(text="Нет курса в Canvas", callback_data=MapCb(exam_id=exam.id, course_id=NO_COURSE))
    kb.adjust(1)
    return "\n".join(lines), kb.as_markup()


@router.message(CommandStart())
async def cmd_start(message: Message, sessionmaker: async_sessionmaker, settings: Settings) -> None:
    await message.answer(
        "Привет! Я твой агент для подготовки к finals. Сначала привяжем экзамены к курсам Canvas."
    )
    text, kb = await _next_question(sessionmaker, settings)
    await message.answer(text, reply_markup=kb)


@router.callback_query(MapCb.filter())
async def on_map_choice(
    query: CallbackQuery,
    callback_data: MapCb,
    sessionmaker: async_sessionmaker,
    settings: Settings,
) -> None:
    course_id = None if callback_data.course_id == NO_COURSE else callback_data.course_id
    async with sessionmaker() as session:
        try:
            exam = await confirm_mapping(session, callback_data.exam_id, course_id)
        except ValueError:
            await query.answer("Этот вариант устарел, начни заново: /start", show_alert=True)
            return
        await session.commit()
    await query.answer("Сохранил")
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
    text, kb = await _next_question(sessionmaker, settings)
    if isinstance(query.message, Message):
        await query.message.answer(f"«{h(exam.title)}» — готово.\n\n{text}", reply_markup=kb)


@router.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer("Пока я умею только /start. Агент и планировщик появятся в Milestone 2.")
