"""Quiz poll answers and /diagnostic."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, PollAnswer

from agent.brain.tool_handlers import ToolError, find_exam
from agent.flows import Services
from agent.prep.diagnostic import maybe_finish, start_diagnostic
from agent.prep.quizzes import ANSWER_LOCK, QuizOutcome, record_answer
from agent.telegram.format import h

log = logging.getLogger(__name__)
router = Router(name="quiz")


def outcome_text(o: QuizOutcome, purpose: str) -> str | None:
    if purpose != "verification":
        return None
    if o.passed:
        text = f"✅ {o.correct}/{o.total} — блок засчитан."
        if o.level_change:
            text += f" Уровень темы: {o.level_change[0]} → {o.level_change[1]}."
        return text
    return (
        f"❌ {o.correct}/{o.total} — засчитана половина блока, тема вернётся завтра на повторение. "
        "Объяснения — под каждым вопросом."
    )


@router.poll_answer()
async def on_poll_answer(answer: PollAnswer, svc: Services) -> None:
    if not answer.option_ids:
        return  # vote retracted (not possible for quiz polls, but harmless)
    async with ANSWER_LOCK, svc.sessionmaker() as s:  # read-modify-write of quiz JSON: serialise
        outcome = await record_answer(s, answer.poll_id, answer.option_ids[0], svc.clock)
        await s.commit()
    if outcome is None:
        return
    text = outcome_text(outcome, outcome.purpose)
    if text:
        await svc.notifier.send(text)
    if outcome.purpose == "diagnostic":
        await maybe_finish(svc, outcome.quiz_id)


@router.message(Command("diagnostic"))
async def cmd_diagnostic(message: Message, command: CommandObject, svc: Services) -> None:
    if not command.args:
        await message.answer(
            "Формат: <code>/diagnostic networks</code> — 2 вопроса на каждую тему курса."
        )
        return
    async with svc.sessionmaker() as s:
        try:
            exam = await find_exam(s, command.args)
        except ToolError as exc:
            await message.answer(h(str(exc)))
            return
    status = await start_diagnostic(svc, exam.id)
    if status:
        await message.answer(h(status))
