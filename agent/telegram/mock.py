"""Mock exam mode in Telegram: /mock or «начинаю past paper» → exam → paper → timer → score →
topics where marks were lost. No LLM involved."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from agent.db.models import CourseFile, Exam, MockExam, Topic
from agent.flows import Services
from agent.planner.render import short_exam
from agent.prep import mock as mock_mod
from agent.telegram.format import fmt_dt, h

router = Router(name="mock")
NO_FILE = 0


class MockCb(CallbackData, prefix="mk"):
    stage: str  # exam | paper
    exam_id: int
    file_id: int = NO_FILE


class MockTopicCb(CallbackData, prefix="mt"):
    mock_id: int
    topic_id: int


class MockCtlCb(CallbackData, prefix="mc"):
    mock_id: int
    action: str  # finish (early) | done (topics chosen)


async def send_exam_choice(message: Message, svc: Services) -> None:
    async with svc.sessionmaker() as s:
        exams = (
            await s.scalars(
                select(Exam).where(Exam.starts_at > svc.clock.now()).order_by(Exam.starts_at)
            )
        ).all()
    kb = InlineKeyboardBuilder()
    for e in exams:
        kb.button(text=short_exam(e.title), callback_data=MockCb(stage="exam", exam_id=e.id))
    kb.adjust(2)
    await message.answer("📝 Mock exam: по какому курсу?", reply_markup=kb.as_markup())


@router.message(Command("mock"))
async def cmd_mock(message: Message, svc: Services) -> None:
    await send_exam_choice(message, svc)


@router.callback_query(MockCb.filter())
async def on_mock_choice(query: CallbackQuery, callback_data: MockCb, svc: Services) -> None:
    await query.answer()
    if not isinstance(query.message, Message):
        return
    if callback_data.stage == "exam":
        async with svc.sessionmaker() as s:
            exam = await s.get(Exam, callback_data.exam_id)
            papers = (
                await s.scalars(
                    select(CourseFile)
                    .where(CourseFile.course_id == exam.course_id, CourseFile.kind == "past_paper")
                    .order_by(CourseFile.id)
                )
            ).all()
        kb = InlineKeyboardBuilder()
        for f in papers[:8]:
            kb.button(
                text=f.display_name[:60],
                callback_data=MockCb(stage="paper", exam_id=exam.id, file_id=f.id),
            )
        kb.button(
            text="Без файла (свой вариант)", callback_data=MockCb(stage="paper", exam_id=exam.id)
        )
        kb.adjust(1)
        await query.message.edit_text(
            f"📝 {h(short_exam(exam.title))}: какой вариант?", reply_markup=kb.as_markup()
        )
        return
    async with svc.sessionmaker() as s:
        m = await mock_mod.start_mock(
            s,
            callback_data.exam_id,
            callback_data.file_id if callback_data.file_id != NO_FILE else None,
            svc.clock,
        )
        await s.commit()
    kb = InlineKeyboardBuilder()
    kb.button(text="Закончил раньше", callback_data=MockCtlCb(mock_id=m.id, action="finish"))
    minutes = int((m.ends_at - m.started_at).total_seconds() // 60)
    await query.message.edit_text(
        f"⏱ Старт! {minutes} мин, до <b>{fmt_dt(m.ends_at, svc.settings.zone, weekday=False)}</b>. "
        "Напомню на половине и за 10 минут. Удачи!",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MockCtlCb.filter())
async def on_mock_ctl(query: CallbackQuery, callback_data: MockCtlCb, svc: Services) -> None:
    async with svc.sessionmaker() as s:
        if callback_data.action == "finish":
            m = await mock_mod.finish_early(s, callback_data.mock_id, svc.clock)
            await s.commit()
            await query.answer()
            if m and isinstance(query.message, Message):
                await query.message.edit_reply_markup(reply_markup=None)
                await query.message.answer(SCORE_QUESTION)
            return
        result = await mock_mod.finish(s, callback_data.mock_id, svc.clock)
        await s.commit()
    await query.answer()
    if result is None or not isinstance(query.message, Message):
        return
    m, weakened = result
    await query.message.edit_reply_markup(reply_markup=None)
    tail = (
        "Слабые темы: " + ", ".join(h(w) for w in weakened) + " — ночью план добавит им блоков."
        if weakened
        else "Без потерь по темам — отлично."
    )
    await query.message.answer(f"Записал mock: {m.score_pct:g}%. {tail}")


SCORE_QUESTION = (
    "⏰ Время! Сколько баллов (примерно)? Напиши число: "
    "<code>72</code>, <code>72%</code> или <code>36/50</code>."
)


async def topics_keyboard(svc: Services, mock: MockExam) -> InlineKeyboardMarkup:
    async with svc.sessionmaker() as s:
        topics = (
            await s.scalars(
                select(Topic)
                .where(Topic.exam_id == mock.exam_id, Topic.status == "active")
                .order_by(Topic.id)
            )
        ).all()
    chosen = set(mock.lost_topics_json or [])
    kb = InlineKeyboardBuilder()
    for t in topics:
        mark = "❌ " if t.id in chosen else ""
        kb.button(
            text=f"{mark}{t.name}"[:60], callback_data=MockTopicCb(mock_id=mock.id, topic_id=t.id)
        )
    kb.button(text="Готово", callback_data=MockCtlCb(mock_id=mock.id, action="done"))
    kb.adjust(1)
    return kb.as_markup()


async def handle_mock_text(message: Message, svc: Services, text: str) -> bool:
    """Called by the chat handler first: mock trigger phrase or a score while one is awaited."""
    if mock_mod.is_trigger(text):
        await send_exam_choice(message, svc)
        return True
    async with svc.sessionmaker() as s:
        m = await mock_mod.active_mock(s)
        if m is None or m.state != "awaiting_score":
            return False
        score = mock_mod.parse_score(text)
        if score is None:
            return False  # not a score → normal chat (the question stays open for 2 h)
        await mock_mod.record_score(s, m, score)
        await s.commit()
    await message.answer(
        f"Записал {score:g}%. На каких темах потерял баллы? Отметь и нажми «Готово».",
        reply_markup=await topics_keyboard(svc, m),
    )
    return True


@router.callback_query(MockTopicCb.filter())
async def on_mock_topic(query: CallbackQuery, callback_data: MockTopicCb, svc: Services) -> None:
    async with svc.sessionmaker() as s:
        await mock_mod.toggle_topic(s, callback_data.mock_id, callback_data.topic_id)
        await s.commit()
        m = await s.get(MockExam, callback_data.mock_id)
    await query.answer()
    if m is not None and m.state == "awaiting_topics" and isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=await topics_keyboard(svc, m))
