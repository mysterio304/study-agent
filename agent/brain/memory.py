"""Conversation memory: last N messages verbatim + a rolling summary of older ones (§9a.4).

Only plain text is stored (no tool or thinking blocks), so replaying history never edits
blocks the API has seen and the cached history prefix stays valid.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.brain.budgets import HISTORY_BUDGET, estimate_tokens, truncate_to_tokens
from agent.core.clock import Clock
from agent.db.models import ChatMessage, ConversationSummary

log = logging.getLogger(__name__)

WINDOW = 8
SUMMARY_TRIGGER = 8  # summarise once this many messages fell out of the window
MAX_MESSAGE_CHARS = 1_500


async def save_message(
    session: AsyncSession, role: str, text: str, clock: Clock, via: str = "text"
) -> None:
    session.add(
        ChatMessage(
            role=role, content_json=text[: MAX_MESSAGE_CHARS * 2], via=via, created_at=clock.now()
        )
    )
    await session.flush()


async def _summary_row(session: AsyncSession, clock: Clock) -> ConversationSummary:
    row = await session.get(ConversationSummary, 1)
    if row is None:
        row = ConversationSummary(id=1, text="", covers_until_message_id=0, updated_at=clock.now())
        session.add(row)
        await session.flush()
    return row


async def history_messages(session: AsyncSession, clock: Clock) -> list[dict[str, str]]:
    """API-ready history: optional summary pair + the last WINDOW messages, within HISTORY_BUDGET."""
    rows = list(
        reversed(
            (
                await session.scalars(
                    select(ChatMessage).order_by(ChatMessage.id.desc()).limit(WINDOW)
                )
            ).all()
        )
    )
    summary = (await _summary_row(session, clock)).text
    msgs: list[dict[str, str]] = []
    if summary:
        msgs += [
            {
                "role": "user",
                "content": f"<summary of earlier conversation>\n{summary}\n</summary>",
            },
            {"role": "assistant", "content": "Понял, учитываю."},
        ]
    for r in rows:
        text = truncate_to_tokens(str(r.content_json), 400)
        if msgs and msgs[-1]["role"] == r.role:  # API needs alternation; merge same-role runs
            msgs[-1] = {"role": r.role, "content": msgs[-1]["content"] + "\n" + text}
        else:
            msgs.append({"role": r.role, "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    keep = 2 if summary else 0  # never drop the summary pair; drop the oldest exchange instead
    while estimate_tokens(msgs) > HISTORY_BUDGET and len(msgs) > keep + 2:
        del msgs[keep : keep + 2]
    if msgs and msgs[-1]["role"] == "user":  # the new user turn is appended by the caller
        msgs.pop()
    return msgs


async def pending_for_summary(session: AsyncSession, clock: Clock) -> list[ChatMessage]:
    """Messages older than the window and not yet in the summary (only when enough piled up)."""
    row = await _summary_row(session, clock)
    newest = (
        await session.scalars(select(ChatMessage.id).order_by(ChatMessage.id.desc()).limit(WINDOW))
    ).all()
    if len(newest) < WINDOW:
        return []
    boundary = min(newest)
    older = list(
        (
            await session.scalars(
                select(ChatMessage)
                .where(ChatMessage.id > row.covers_until_message_id, ChatMessage.id < boundary)
                .order_by(ChatMessage.id)
            )
        ).all()
    )
    return older if len(older) >= SUMMARY_TRIGGER else []


async def store_summary(session: AsyncSession, text: str, until_id: int, clock: Clock) -> None:
    row = await _summary_row(session, clock)
    row.text = truncate_to_tokens(text, 500)
    row.covers_until_message_id = until_id
    row.updated_at = clock.now()
    await session.flush()


def summary_prompt(previous: str, older: list[ChatMessage]) -> str:
    convo = "\n".join(f"{m.role}: {str(m.content_json)[:600]}" for m in older)
    return (
        "Update the running summary of a chat between a student and their study agent. "
        "Keep decisions, agreements, preferences, open questions and facts about progress. "
        "Drop small talk. Russian, max 120 words, plain text.\n\n"
        f"Previous summary:\n{previous or '(none)'}\n\nNew messages:\n{convo}"
    )
