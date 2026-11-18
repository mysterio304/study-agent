"""Telegram handlers driven with lightweight fakes (no network): commands, mock flow, uploads,
poll answers, setup wizard."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.filters import CommandObject
from sqlalchemy import select

from agent.canvas.fake import FakeCanvasSource
from agent.canvas.sync import sync_courses
from agent.config import Settings
from agent.core.clock import FixedClock
from agent.db.models import CourseFile, Exam, MockExam, Quiz, Topic
from agent.flows import Services, apply_choice, propose_replan
from agent.prep.mapping_service import confirm_mapping, suggestions_for_unconfirmed
from agent.prep.quizzes import start_quiz, store_questions
from agent.prep.seed import seed_exams
from agent.telegram import chat as chat_module
from agent.telegram import commands, files, mock, quiz, setup_wizard
from tests.files.samples import make_pdf
from tests.notify.test_jobs import Recorder
from tests.planner.conftest import HKT, at


class FakeBot:
    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload

    async def send_chat_action(self, *a: Any, **k: Any) -> None:
        return None

    async def download(self, file: Any, destination: Any) -> None:
        if isinstance(destination, (str, Path)):
            Path(destination).write_bytes(self.payload)
        else:
            destination.write(self.payload)


class FakeMessage:
    """Duck-types the parts of aiogram's Message our handlers use (passes isinstance checks)."""

    def __init__(self, text: str = "", bot: FakeBot | None = None, document: Any = None) -> None:
        self.text, self.bot, self.document = text, bot or FakeBot(), document
        self.chat = SimpleNamespace(id=1)
        self.sent: list[tuple[str, Any]] = []

    async def answer(self, text: str, reply_markup: Any = None, **kw: Any) -> None:
        self.sent.append((text, reply_markup))

    async def edit_text(self, text: str, reply_markup: Any = None, **kw: Any) -> None:
        self.sent.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup: Any = None) -> None:
        return None

    def buttons(self, i: int = -1) -> list[str]:
        kb = self.sent[i][1]
        return [b.text for row in kb.inline_keyboard for b in row] if kb else []

    def callbacks(self, i: int = -1) -> list[str]:
        kb = self.sent[i][1]
        return [b.callback_data for row in kb.inline_keyboard for b in row] if kb else []


@pytest.fixture(autouse=True)
def _accept_fake_messages(monkeypatch):
    """Handlers check `isinstance(query.message, Message)`; let our fake pass that check."""
    for module in (setup_wizard, mock, files, chat_module):
        monkeypatch.setattr(module, "Message", FakeMessage)


class FakeQuery:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message
        self.answers: list[str] = []

    async def answer(self, text: str | None = None, **kw: Any) -> None:
        self.answers.append(text or "")


@pytest.fixture
def clk() -> FixedClock:
    return FixedClock(at(date(2026, 11, 19), 10), HKT)


@pytest.fixture
async def svc(sessionmaker, clk, tmp_path) -> Services:
    async with sessionmaker() as s:
        await seed_exams(s)
        await sync_courses(s, FakeCanvasSource(), clk)
        for exam, sug in await suggestions_for_unconfirmed(s):
            await confirm_mapping(s, exam.id, sug.course_id)
        for eid in (1, 2, 3, 4):
            s.add(
                Topic(
                    exam_id=eid,
                    name=f"Topic {eid}",
                    effort_hours=2,
                    importance=2,
                    level=1,
                    target_level=2,
                    status="active",
                )
            )
        await s.commit()
    services = Services(
        Settings(_env_file=None),
        sessionmaker,
        clk,
        FakeCanvasSource(),
        Recorder(),
        data_dir=tmp_path,
    )
    await apply_choice(services, await propose_replan(services, "x", send=False), "A")
    return services


def msg(text: str = "", **kw: Any) -> FakeMessage:
    return FakeMessage(text, **kw)


def cmd(name: str, args: str | None = None) -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args)


async def test_read_only_commands_render_templates(svc) -> None:
    for handler in (
        commands.cmd_today,
        commands.cmd_plan,
        commands.cmd_progress,
        commands.cmd_deadlines,
        commands.cmd_grades,
    ):
        m = msg()
        await handler(m, svc)
        assert m.sent and m.sent[0][0]
    m = msg()
    await commands.cmd_need(m, cmd("need", "networks A-"), svc)
    assert "Networks" in m.sent[0][0] and "A-" in m.sent[0][0]
    m = msg()
    await commands.cmd_need(m, cmd("need", "quantum basket weaving"), svc)
    assert "не найден" in m.sent[0][0]


async def test_settings_command_shows_and_changes_values(svc) -> None:
    m = msg()
    await commands.cmd_settings(m, cmd("settings"), svc)
    assert "🔒" in m.sent[0][0] and "cap_hours" in m.sent[0][0]
    m = msg()
    await commands.cmd_settings(m, cmd("settings", "cap_hours 99"), svc)
    assert "максимум" in m.sent[0][0]
    m = msg()
    await commands.cmd_settings(m, cmd("settings", "cap_hours 5"), svc)
    assert "Готово" in m.sent[0][0]


async def test_mock_flow_through_handlers(svc, clk) -> None:
    m = msg("начинаю past paper")
    assert await mock.handle_mock_text(m, svc, m.text)
    exam_cb = mock.MockCb.unpack(m.callbacks()[0])
    await mock.on_mock_choice(FakeQuery(m), exam_cb, svc)
    paper_cb = mock.MockCb.unpack(m.callbacks()[-1])
    assert paper_cb.stage == "paper"
    await mock.on_mock_choice(FakeQuery(m), paper_cb, svc)
    assert "Старт" in m.sent[-1][0]
    finish_cb = mock.MockCtlCb.unpack(m.callbacks()[-1])
    await mock.on_mock_ctl(FakeQuery(m), finish_cb, svc)
    assert "Сколько баллов" in m.sent[-1][0]
    m2 = msg("перенеси на 2")  # not a bare score → goes to the agent, not recorded as 2 %
    assert not await mock.handle_mock_text(m2, svc, m2.text) and m2.sent == []
    m3 = msg("36/50")
    assert await mock.handle_mock_text(m3, svc, m3.text)
    topic_cb = mock.MockTopicCb.unpack(m3.callbacks()[0])
    await mock.on_mock_topic(FakeQuery(m3), topic_cb, svc)
    done_cb = mock.MockCtlCb.unpack(m3.callbacks()[-1])
    await mock.on_mock_ctl(FakeQuery(m3), done_cb, svc)
    assert "Записал mock: 72%" in m3.sent[-1][0]
    async with svc.sessionmaker() as s:
        assert (await s.scalar(select(MockExam))).state == "done"
    assert not await mock.handle_mock_text(msg("привет"), svc, "привет")


async def test_document_upload_indexes_past_paper(svc, tmp_path) -> None:
    pdf = make_pdf(tmp_path / "src.pdf", ["Final 2024: TCP congestion control question"])
    doc = SimpleNamespace(file_name="Final 2024.pdf", file_size=1000, file_unique_id="abc")
    m = msg(bot=FakeBot(pdf.read_bytes()), document=doc)
    await files.on_document(m, svc)
    cb = files.UploadCb.unpack(m.callbacks()[0])
    assert cb.kind == "past_paper"
    await files.on_upload_course(FakeQuery(m), cb, svc)
    assert "проиндексирован" in m.sent[-1][0] or "Важность" in m.sent[-1][0]
    async with svc.sessionmaker() as s:
        row = await s.scalar(select(CourseFile).where(CourseFile.source == "telegram_upload"))
        assert row.index_state == "indexed" and row.kind == "past_paper"
    m = msg(document=SimpleNamespace(file_name="song.mp3", file_size=10, file_unique_id="x"))
    await files.on_document(m, svc)
    assert "PDF, PPTX и DOCX" in m.sent[0][0]
    m = msg(document=SimpleNamespace(file_name="big.pdf", file_size=50_000_000, file_unique_id="y"))
    await files.on_document(m, svc)
    assert "20 МБ" in m.sent[0][0]


async def test_poll_answer_completes_quiz_and_reports(svc, clk) -> None:
    async with svc.sessionmaker() as s:
        topic = await s.scalar(select(Topic))
        await store_questions(
            s,
            topic.id,
            [
                {
                    "question": f"Q{i}",
                    "options": ["a", "b"],
                    "correct": 0,
                    "explanation": "",
                    "source": "",
                }
                for i in range(3)
            ],
            "batch",
            clk,
        )
        quiz_row = await start_quiz(s, topic.id, clk, purpose="adhoc", block_id=None, n=3)
        quiz_row.questions_json = [
            {**x, "poll_id": f"p{i}"} for i, x in enumerate(quiz_row.questions_json)
        ]
        await s.commit()
    for i in range(3):
        await quiz.on_poll_answer(SimpleNamespace(poll_id=f"p{i}", option_ids=[0]), svc)
    await quiz.on_poll_answer(
        SimpleNamespace(poll_id="p0", option_ids=[]), svc
    )  # retraction ignored
    async with svc.sessionmaker() as s:
        assert (await s.scalar(select(Quiz))).passed is True
    m = msg()
    await quiz.cmd_diagnostic(m, cmd("diagnostic"), svc)
    assert "Формат" in m.sent[0][0]
    m = msg()
    await quiz.cmd_diagnostic(m, cmd("diagnostic", "networks"), svc)
    assert "ANTHROPIC_API_KEY" in m.sent[0][0]


async def test_setup_wizard_mapping_question(sessionmaker, clk, tmp_path) -> None:
    async with sessionmaker() as s:
        await seed_exams(s)
        await sync_courses(s, FakeCanvasSource(), clk)
        await s.commit()
    services = Services(Settings(_env_file=None), sessionmaker, clk, FakeCanvasSource(), Recorder())
    m = msg("/start")
    await setup_wizard.cmd_start(m, services)
    assert "Computer Networks" in m.sent[1][0] and "✅ Да" in m.buttons(1)
    cb = setup_wizard.MapCb.unpack(m.callbacks(1)[0])
    await setup_wizard.on_map_choice(FakeQuery(m), cb, services)
    async with sessionmaker() as s:
        assert (await s.scalar(select(Exam).where(Exam.id == cb.exam_id))).mapping_confirmed
