"""Files you send to the bot (SPEC §7, your answer 12): PDF/PPTX/DOCX → pick the course → indexed
as a past paper (or course material) and used for search, quizzes and mock exams."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from agent.db.models import CourseFile, Exam, Setting
from agent.files.extract import kind_of
from agent.files.indexer import index_local_file, safe_name
from agent.flows import Services, propose_importance
from agent.planner.render import short_exam
from agent.telegram.format import h

log = logging.getLogger(__name__)
router = Router(name="files")

TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API getFile limit
PENDING_KEY = "pending_uploads"


class UploadCb(CallbackData, prefix="up"):
    token: str
    exam_id: int
    kind: str  # past_paper | lecture


@router.message(F.document)
async def on_document(message: Message, svc: Services) -> None:
    doc = message.document
    assert doc is not None
    name = doc.file_name or "file"
    if kind_of(name) is None:
        await message.answer("Я читаю PDF, PPTX и DOCX. Этот файл пропущу.")
        return
    if (doc.file_size or 0) > TELEGRAM_DOWNLOAD_LIMIT:
        await message.answer(
            "Файл больше 20 МБ — Telegram не даёт боту его скачать. Сожми или разбей."
        )
        return
    token = hashlib.sha256(doc.file_unique_id.encode()).hexdigest()[:12]
    dest = svc.data_dir / "files" / "uploads" / f"{token}-{safe_name(name)}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    await message.bot.download(doc, destination=dest)
    async with svc.sessionmaker() as s:
        row = await s.get(Setting, PENDING_KEY) or Setting(key=PENDING_KEY, value_json={})
        row.value_json = {**(row.value_json or {}), token: {"path": str(dest), "name": name}}
        row.updated_at, row.updated_by = svc.clock.now(), "user"
        s.add(row)
        exams = (
            await s.scalars(
                select(Exam).where(Exam.course_id.is_not(None)).order_by(Exam.starts_at)
            )
        ).all()
        await s.commit()
    kb = InlineKeyboardBuilder()
    for e in exams:
        kb.button(
            text=f"📝 {short_exam(e.title)}: past paper",
            callback_data=UploadCb(token=token, exam_id=e.id, kind="past_paper"),
        )
        kb.button(
            text=f"📚 {short_exam(e.title)}: материалы",
            callback_data=UploadCb(token=token, exam_id=e.id, kind="lecture"),
        )
    kb.adjust(2)
    await message.answer(
        f"Получил «{h(name)}». К какому курсу и что это?", reply_markup=kb.as_markup()
    )


@router.callback_query(UploadCb.filter())
async def on_upload_course(query: CallbackQuery, callback_data: UploadCb, svc: Services) -> None:
    async with svc.sessionmaker() as s:
        pending = await s.get(Setting, PENDING_KEY)
        item = (pending.value_json or {}).get(callback_data.token) if pending else None
        exam = await s.get(Exam, callback_data.exam_id)
        if item is None or exam is None or exam.course_id is None:
            await query.answer("Файл уже обработан или курс не найден", show_alert=True)
            return
        row = CourseFile(
            course_id=exam.course_id,
            display_name=item["name"][:500],
            source="telegram_upload",
            kind=callback_data.kind
            if callback_data.kind in ("past_paper", "lecture")
            else "lecture",
            index_state="pending",
        )
        s.add(row)
        pending.value_json = {
            k: v for k, v in pending.value_json.items() if k != callback_data.token
        }
        await s.commit()
    await query.answer("Индексирую…")
    if isinstance(query.message, Message):
        await query.message.edit_reply_markup(reply_markup=None)
    result = await index_local_file(svc.sessionmaker, row.id, Path(item["path"]), svc.clock)
    if isinstance(query.message, Message):
        if result.state == "indexed" and callback_data.kind == "past_paper":
            await query.message.answer(
                f"✅ «{h(result.name)}» проиндексирован: {result.chunks} фрагментов."
            )
            if await propose_importance(svc, row.course_id) is None:
                await query.message.answer("Важность тем по этому past paper не меняется.")
        elif result.state == "indexed":
            await query.message.answer(
                f"✅ «{h(result.name)}» проиндексирован: {result.chunks} фрагментов. "
                "Использую для поиска, квизов и mock-экзаменов."
            )
        elif result.state == "no_text":
            await query.message.answer(
                "В файле нет текстового слоя (скан?) — без OCR я его не прочитаю."
            )
        else:
            await query.message.answer("Не получилось прочитать файл (повреждён или зашифрован).")
