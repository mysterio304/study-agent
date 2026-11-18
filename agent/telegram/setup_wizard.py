"""/start wizard: (1) confirm exam ↔ Canvas course mapping, (2) review discovered topics,
(3) self-rate each topic, (4) confirm the first plan. Diagnostic quizzes come in M5."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from agent.flows import Services, continue_setup, on_topics_ready, pending_drafts
from agent.prep import topics_service as ts
from agent.prep.mapping_service import confirm_mapping, suggestions_for_unconfirmed
from agent.telegram.format import fmt_dt, h
from agent.telegram.keyboards import RateCb, TopicCb

router = Router(name="setup_wizard")

NO_COURSE = 0


class MapCb(CallbackData, prefix="map"):
    exam_id: int
    course_id: int  # NO_COURSE == "no Canvas course"


async def _mapping_question(svc: Services) -> tuple[str, InlineKeyboardMarkup] | None:
    async with svc.sessionmaker() as session:
        pending = await suggestions_for_unconfirmed(session)
    if not pending:
        return None
    exam, s = pending[0]
    kb = InlineKeyboardBuilder()
    lines = [f"<b>Экзамен:</b> {h(exam.title)} — {fmt_dt(exam.starts_at, svc.settings.zone)}"]
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


async def _advance(svc: Services, message: Message) -> None:
    question = await _mapping_question(svc)
    if question is not None:
        await message.answer(question[0], reply_markup=question[1])
        return
    drafts = await pending_drafts(svc)
    if drafts:
        for exam_id in drafts:
            await on_topics_ready(svc, exam_id)
        return
    if svc.discovery is not None:
        if await svc.discovery.in_progress():
            await message.answer("🔎 Темы ещё собираются — пришлю списки, как только будут готовы.")
            return
        started = await svc.discovery.start()
        if started:
            await message.answer(
                "🔎 Собираю темы из модулей, файлов и syllabus. Пришлю списки на проверку, "
                "обычно в течение 15–20 минут."
            )
            return
    await continue_setup(svc)


@router.message(CommandStart())
async def cmd_start(message: Message, svc: Services) -> None:
    await message.answer(
        "Привет! Я агент для подготовки к finals. Настроим всё за несколько шагов."
    )
    await _advance(svc, message)


@router.callback_query(MapCb.filter())
async def on_map_choice(query: CallbackQuery, callback_data: MapCb, svc: Services) -> None:
    course_id = None if callback_data.course_id == NO_COURSE else callback_data.course_id
    async with svc.sessionmaker() as session:
        try:
            exam = await confirm_mapping(session, callback_data.exam_id, course_id)
        except ValueError:
            await query.answer("Этот вариант устарел, начни заново: /start", show_alert=True)
            return
        await session.commit()
    await query.answer("Сохранил")
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.answer(f"«{h(exam.title)}» — готово.")
        await _advance(svc, query.message)


@router.callback_query(TopicCb.filter())
async def on_topics(query: CallbackQuery, callback_data: TopicCb, svc: Services) -> None:
    if callback_data.action == "edit":
        await query.answer()
        if isinstance(query.message, Message):
            await query.message.answer(
                "Пиши правки обычным текстом (например «убери Wireless»). "
                "Когда закончишь — нажми «Темы верные» под списком."
            )
        return
    async with svc.sessionmaker() as session:
        n = await ts.accept_topics(session, callback_data.exam_id)
        await session.commit()
    await query.answer(f"Принял {n} тем")
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
    await continue_setup(svc)


@router.callback_query(RateCb.filter())
async def on_rate(query: CallbackQuery, callback_data: RateCb, svc: Services) -> None:
    async with svc.sessionmaker() as session:
        try:
            topic = await ts.rate_topic(session, callback_data.topic_id, callback_data.level)
        except ValueError:
            await query.answer("Уже оценено", show_alert=False)
            return
        if callback_data.rest:
            await ts.rate_rest_of_exam(session, topic.exam_id, callback_data.level)
        await session.commit()
    await query.answer("Ок")
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
    await continue_setup(svc)
