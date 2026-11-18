"""Sending a quiz as Telegram polls. Each poll id is stored right after its send, so an answer that
arrives while later polls are still going out is never lost; if a send fails, the quiz is closed
as unverified (it could never be completed)."""

from __future__ import annotations

from agent.db.models import Quiz
from agent.flows import Services
from agent.prep.quizzes import finalize


async def send_quiz_polls(svc: Services, quiz_id: int) -> bool:
    async with svc.sessionmaker() as s:
        questions = list((await s.get(Quiz, quiz_id)).questions_json)
    for i, q in enumerate(questions):
        explanation = f"{q.get('explanation', '')} ({q.get('source', '')})".strip()[:200]
        poll_id = await svc.notifier.send_poll(
            q["question"], q["options"], q["correct"], explanation
        )
        async with svc.sessionmaker() as s:
            quiz = await s.get(Quiz, quiz_id)
            current = list(quiz.questions_json)
            current[i] = {**current[i], "poll_id": poll_id}
            quiz.questions_json = current
            if poll_id is None:  # Telegram refused: the quiz can't be completed
                await finalize(s, quiz, svc.clock, expired=True)
            await s.commit()
        if poll_id is None:
            return False
    return True
