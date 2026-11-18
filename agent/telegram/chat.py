"""Free-form chat, proposal buttons and block buttons."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from agent.brain.llm import BudgetExceeded
from agent.db.models import Assignment, Reminder
from agent.flows import Services, apply_choice, send_proposal
from agent.planner.blocks import BlockError, auto_move_missed, set_block_status
from agent.planner.render import fmt_day
from agent.planner.service import load_inputs
from agent.prep import checkin
from agent.prep.settings_service import effective
from agent.telegram.format import h
from agent.telegram.keyboards import BlockCb, CheckinCb, DeadlineCb, PropCb
from agent.telegram.mock import handle_mock_text
from agent.voice.service import wants_voice

log = logging.getLogger(__name__)
router = Router(name="chat")

_ALLOWED_TAG = re.compile(r"&lt;(/?)(b|i)&gt;")


def safe_html(text: str) -> str:
    """Escape model output for parse_mode=HTML and re-allow only balanced <b>/<i>.
    Unbalanced or mis-nested tags (e.g. output cut at max_tokens) → tags are dropped."""
    escaped = _ALLOWED_TAG.sub(r"<\1\2>", h(text))
    stack: list[str] = []
    for closing, tag in re.findall(r"<(/?)(b|i)>", escaped):
        if not closing:
            stack.append(tag)
        elif not stack or stack.pop() != tag:
            return h(re.sub(r"</?(b|i)>", "", text))
    return escaped if not stack else h(re.sub(r"</?(b|i)>", "", text))


async def _ack(query: CallbackQuery, reply: str) -> None:
    """Answer a button press. The DB change is already committed, so Telegram errors from stale
    or double-tapped buttons (e.g. "message is not modified") are logged, never raised."""
    try:
        await query.answer(reply[:190])
        if isinstance(query.message, Message):
            await query.message.edit_reply_markup(reply_markup=None)
            await query.message.answer(h(reply))
    except TelegramBadRequest as exc:
        log.info("Ignoring Telegram error on button ack: %s", exc.message)


@router.callback_query(PropCb.filter())
async def on_proposal(query: CallbackQuery, callback_data: PropCb, svc: Services) -> None:
    status = await apply_choice(svc, callback_data.id, callback_data.opt)
    await _ack(query, status)


@router.callback_query(BlockCb.filter())
async def on_block(query: CallbackQuery, callback_data: BlockCb, svc: Services) -> None:
    action, reply = callback_data.action, ""
    async with svc.sessionmaker() as s:
        try:
            if action == "start":
                await set_block_status(s, callback_data.id, "started", svc.clock)
                reply = "Поехали! 50 минут фокуса."
            elif action == "done":
                await set_block_status(s, callback_data.id, "done", svc.clock)
                reply = "Записал ✅"
            elif action == "fail":
                await set_block_status(s, callback_data.id, "skipped", svc.clock)
                reply = "Ок, блок ушёл в долг — ночью перераспределю."
            elif action == "move":  # the student's own explicit request → no extra confirmation
                inputs = await load_inputs(s, svc.settings, svc.clock)
                moved = await auto_move_missed(s, callback_data.id, inputs, svc.clock)
                if moved:
                    when = moved.starts_at.astimezone(svc.settings.zone)
                    reply = f"Перенёс на {fmt_day(when.date())} {when:%H:%M}."
                else:
                    reply = "Свободного слота сегодня/завтра нет — блок ушёл в долг."
                    await set_block_status(s, callback_data.id, "skipped", svc.clock)
            await s.commit()
        except BlockError as exc:
            reply = str(exc)
    await _ack(query, reply)


SNOOZE = timedelta(hours=2)


@router.callback_query(DeadlineCb.filter())
async def on_deadline(query: CallbackQuery, callback_data: DeadlineCb, svc: Services) -> None:
    async with svc.sessionmaker() as s:
        a = await s.get(Assignment, callback_data.id)
        if a is None:
            await query.answer("Задание не найдено")
            return
        if callback_data.action == "done":
            a.user_state, reply = "done", "Отметил как сделанное — больше не напомню."
        elif callback_data.action == "ignore":
            a.user_state, reply = "ignored", "Ок, по этому заданию напоминаний больше не будет."
        else:
            s.add(
                Reminder(
                    text=f"Дедлайн: {a.name}",
                    remind_at=svc.clock.now() + SNOOZE,
                    status="pending",
                    source="deadline_snooze",
                    created_at=svc.clock.now(),
                )
            )
            reply = "Напомню через 2 часа."
        await s.commit()
    await _ack(query, reply)


@router.callback_query(CheckinCb.filter())
async def on_checkin_later(query: CallbackQuery) -> None:
    await query.answer("Ок, спрошу утром.")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, svc: Services) -> None:
    await handle_user_text(message, svc, message.text or "", via="text")


async def handle_user_text(message: Message, svc: Services, text: str, *, via: str) -> None:
    """Shared by text and voice: mock-exam flow, then check-in answer, otherwise the agent. Voice
    replies are a short gist; the full answer is always sent as text too."""
    if await handle_mock_text(message, svc, text):
        return
    if svc.agent is None or svc.llm is None:
        await message.answer(
            "Агент не настроен (нет ANTHROPIC_API_KEY). Команды /today, /plan, /progress работают."
        )
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    try:
        if await _try_checkin(message, svc, text):
            return
        result = await svc.agent.handle(text, via=via)
    except Exception:
        log.exception("Agent turn failed")
        await message.answer("Что-то пошло не так на моей стороне. Попробуй ещё раз через минуту.")
        return
    try:
        await message.answer(safe_html(result.text))
    except TelegramBadRequest:
        log.warning("Telegram rejected formatted reply; sending plain text")
        await message.answer(h(result.text), parse_mode=None)
    finally:
        for pid in result.proposals:  # proposals are already in the DB — always show the cards
            await send_proposal(svc, pid)
    await _maybe_speak(message, svc, text, result.voice_text, result.text, via=via)


async def _maybe_speak(
    message: Message, svc: Services, user_text: str, voice_text: str | None, reply: str, *, via: str
) -> None:
    """Voice out only when asked (tool or «ответь голосом») or in voice mode after voice input."""
    if svc.voice is None:
        return
    if voice_text is None:
        async with svc.sessionmaker() as s:
            voice_mode = bool((await effective(s, svc.settings))["voice_mode"])
        if not (wants_voice(user_text) or (via == "voice" and voice_mode)):
            return
    try:  # voice is a bonus, sent after the text: nothing here may break the turn
        audio, notice = await svc.voice.speak(voice_text or reply)
        if audio:
            await message.answer_voice(BufferedInputFile(audio, filename="reply.ogg"))
        if notice:
            await message.answer(h(notice))
    except Exception:
        log.exception("Voice reply failed")


async def _try_checkin(message: Message, svc: Services, text: str) -> bool:
    """If a check-in is open and the text looks like a report, apply it. Returns True if handled."""
    assert svc.llm is not None
    async with svc.sessionmaker() as s:
        day = await checkin.awaiting(s, svc.clock)
    if day is None or not checkin.looks_like_report(text):
        return False
    try:
        outcome = await checkin.handle_answer(svc.sessionmaker, svc.llm, svc.clock, day, text)
    except BudgetExceeded:
        return False  # the agent replies with the over-budget template
    if outcome.is_report:
        await message.answer(h(outcome.summary))
    return outcome.is_report
