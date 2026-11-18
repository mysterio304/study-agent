"""ORM models. See docs/ARCHITECTURE.md §7 for the schema rationale.

Conventions:
- every `*_at` column is a `UTCDateTime` (aware UTC in Python, naive UTC in SQLite);
- Canvas entities use the Canvas id as primary key;
- enum-like columns are short strings validated in the domain layer (SQLite has no enums).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agent.db.types import UTCDateTime


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict[str, Any]: JSON, list[Any]: JSON}  # noqa: RUF012


# --- Config & time --------------------------------------------------------------------------


class Setting(Base):
    """Runtime settings layered over .env defaults. Locked keys change only via /settings."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime]
    updated_by: Mapped[str] = mapped_column(String(16), default="env")


class BusySlot(Base):
    """Recurring (weekday) or one-off (date) time the planner must not use, e.g. lectures."""

    __tablename__ = "busy_slots"
    id: Mapped[int] = mapped_column(primary_key=True)
    weekday: Mapped[int | None] = mapped_column(Integer)  # 0 = Monday
    on_date: Mapped[date | None] = mapped_column(Date)
    start_local: Mapped[str] = mapped_column(String(5))  # HH:MM
    end_local: Mapped[str] = mapped_column(String(5))
    label: Mapped[str] = mapped_column(String(120))
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)


# --- Canvas mirror --------------------------------------------------------------------------


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255))
    course_code: Mapped[str | None] = mapped_column(String(64))
    current_score: Mapped[float | None] = mapped_column(Float)
    current_grade: Mapped[str | None] = mapped_column(String(8))
    apply_group_weights: Mapped[bool] = mapped_column(Boolean, default=False)
    syllabus_html: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    synced_at: Mapped[datetime]


class AssignmentGroup(Base):
    __tablename__ = "assignment_groups"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    group_weight: Mapped[float | None] = mapped_column(Float)
    rules_json: Mapped[dict[str, Any] | None]
    content_hash: Mapped[str] = mapped_column(String(64))


class Assignment(Base):
    __tablename__ = "assignments"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("assignment_groups.id"))
    name: Mapped[str] = mapped_column(String(255))
    due_at: Mapped[datetime | None] = mapped_column(index=True)
    points_possible: Mapped[float | None] = mapped_column(Float)
    html_url: Mapped[str | None] = mapped_column(String(500))
    submission_state: Mapped[str | None] = mapped_column(String(32))
    submitted_at: Mapped[datetime | None]
    score: Mapped[float | None] = mapped_column(Float)
    grade: Mapped[str | None] = mapped_column(String(32))
    grader_comment_count: Mapped[int] = mapped_column(Integer, default=0)
    is_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    omit_from_final_grade: Mapped[bool] = mapped_column(Boolean, default=False)
    submission_types: Mapped[list[Any] | None]
    excused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    stats_json: Mapped[dict[str, Any] | None]
    user_state: Mapped[str | None] = mapped_column(String(16))  # done / ignored (bot buttons)
    content_hash: Mapped[str] = mapped_column(String(64))
    synced_at: Mapped[datetime]


class Announcement(Base):
    __tablename__ = "announcements"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    message_html: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None]
    html_url: Mapped[str | None] = mapped_column(String(500))
    summary_ru: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    subject: Mapped[str | None] = mapped_column(String(500))
    last_message: Mapped[str | None] = mapped_column(Text)
    last_message_at: Mapped[datetime | None]
    workflow_state: Mapped[str | None] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(64))


class CourseFile(Base):
    __tablename__ = "course_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    canvas_file_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(500))
    content_type: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int | None] = mapped_column(Integer)
    canvas_updated_at: Mapped[datetime | None]
    source: Mapped[str] = mapped_column(String(16))  # files_api / module_item / telegram_upload
    kind: Mapped[str] = mapped_column(String(16), default="other")
    module_name: Mapped[str | None] = mapped_column(String(255))
    local_path: Mapped[str | None] = mapped_column(String(500))
    sha256: Mapped[str | None] = mapped_column(String(64))
    index_state: Mapped[str] = mapped_column(String(16), default="pending")
    indexed_at: Mapped[datetime | None]


class CourseModule(Base):
    """Canvas module headers — used for topic discovery even when files are hidden."""

    __tablename__ = "course_modules"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    position: Mapped[int | None] = mapped_column(Integer)
    items_json: Mapped[list[Any] | None]
    content_hash: Mapped[str] = mapped_column(String(64))


class SyncState(Base):
    """Per-scope sync bookkeeping. `initialized=False` → the next sync seeds silently."""

    __tablename__ = "sync_state"
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    last_success_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


# --- Finals prep ----------------------------------------------------------------------------


class Exam(Base):
    __tablename__ = "exams"
    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"))
    mapping_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(255), unique=True)
    starts_at: Mapped[datetime]
    duration_min: Mapped[int] = mapped_column(Integer)
    final_weight: Mapped[float | None] = mapped_column(Float)  # 0..1
    weight_source: Mapped[str] = mapped_column(String(16), default="unknown")
    target_grade: Mapped[str] = mapped_column(String(4), default="A-")
    location: Mapped[str | None] = mapped_column(String(255))


class Topic(Base):
    __tablename__ = "topics"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    source_refs_json: Mapped[list[Any] | None]
    effort_hours: Mapped[float] = mapped_column(Float, default=3.0)
    importance: Mapped[int] = mapped_column(Integer, default=2)  # 1..3
    level: Mapped[int] = mapped_column(Integer, default=0)  # 0..3
    target_level: Mapped[int] = mapped_column(Integer, default=2)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"))
    last_studied_at: Mapped[datetime | None]
    next_review_at: Mapped[datetime | None]
    review_step: Mapped[int] = mapped_column(Integer, default=0)
    # Study minutes logged since the level last changed; reduces the planner's remaining effort.
    progress_min: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    level_changed_at: Mapped[datetime | None]


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime]
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("plan_versions.id"))
    reason: Mapped[str] = mapped_column(Text)
    proposed_by: Mapped[str] = mapped_column(String(16))  # planner / agent / user / auto
    applied_at: Mapped[datetime | None]
    snapshot_json: Mapped[dict[str, Any]]  # {"blocks": [...future blocks], "overrides": {...}}


class Block(Base):
    __tablename__ = "blocks"
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_version_id: Mapped[int] = mapped_column(ForeignKey("plan_versions.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(index=True)
    ends_at: Mapped[datetime]
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"))
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"))
    kind: Mapped[str] = mapped_column(String(16))  # learn / practice / review / mock
    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    moved_to_block_id: Mapped[int | None] = mapped_column(ForeignKey("blocks.id"))
    ping_count: Mapped[int] = mapped_column(Integer, default=0)
    last_ping_at: Mapped[datetime | None]
    verification_quiz_id: Mapped[int | None] = mapped_column(ForeignKey("quizzes.id"))


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(16))  # plan / settings / topics / drop
    summary_ru: Mapped[str] = mapped_column(Text)
    options_json: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    chosen_option: Mapped[str | None] = mapped_column(String(8))
    expires_at: Mapped[datetime | None]
    tg_message_id: Mapped[int | None] = mapped_column(Integer)


class StudySession(Base):
    __tablename__ = "study_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"), index=True)
    block_id: Mapped[int | None] = mapped_column(ForeignKey("blocks.id"))
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    minutes: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    quiz_score: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))  # block / checkin / manual / mock
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class Checkin(Base):
    __tablename__ = "checkins"
    date_local: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    raw_text: Mapped[str | None] = mapped_column(Text)
    parsed_json: Mapped[dict[str, Any] | None]
    completed_at: Mapped[datetime | None]


class Quiz(Base):
    __tablename__ = "quizzes"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), index=True)
    block_id: Mapped[int | None] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(16))  # verification / diagnostic / adhoc
    questions_json: Mapped[list[Any]]
    answers_json: Mapped[list[Any] | None]
    score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]


class MockExam(Base):
    __tablename__ = "mock_exams"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"))
    file_id: Mapped[int | None] = mapped_column(ForeignKey("course_files.id"))
    started_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    score_pct: Mapped[float | None] = mapped_column(Float)
    lost_topics_json: Mapped[list[Any] | None]
    # running → awaiting_score → awaiting_topics → done (or cancelled)
    state: Mapped[str] = mapped_column(String(16), default="running", server_default="running")
    pings_sent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Agreement(Base):
    __tablename__ = "agreements"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    exam_id: Mapped[int | None] = mapped_column(ForeignKey("exams.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    valid_until: Mapped[datetime | None]
    created_at: Mapped[datetime]


# --- Notifications & reminders --------------------------------------------------------------


class OutboxMessage(Base):
    __tablename__ = "outbox"
    __table_args__ = (UniqueConstraint("dedupe_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(128))
    urgency: Mapped[str] = mapped_column(String(8))  # urgent / normal / digest
    text: Mapped[str] = mapped_column(Text)
    buttons_json: Mapped[list[Any] | None]
    as_voice: Mapped[bool] = mapped_column(Boolean, default=False)
    not_before: Mapped[datetime] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None]
    tg_message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime]


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    remind_at: Mapped[datetime] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    snoozed_from_id: Mapped[int | None] = mapped_column(ForeignKey("reminders.id"))
    source: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime]


# --- Brain ----------------------------------------------------------------------------------


class ChatMessage(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    content_json: Mapped[Any] = mapped_column(JSON)
    via: Mapped[str] = mapped_column(String(8), default="text")
    created_at: Mapped[datetime] = mapped_column(index=True)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(index=True)
    model: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32), index=True)  # call type
    route_reason: Mapped[str | None] = mapped_column(String(64))
    prefix_key: Mapped[str | None] = mapped_column(String(64))  # model + toolset profile
    batch: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_5m_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_1h_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class ConversationSummary(Base):
    """Rolling summary of chat history older than the live window (single row, id=1)."""

    __tablename__ = "conversation_summary"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    covers_until_message_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime]


class QuizQuestion(Base):
    """Per-topic question bank; questions are reused before new ones are generated."""

    __tablename__ = "quiz_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), index=True)
    question_json: Mapped[dict[str, Any]]
    source_chunk_ids: Mapped[list[Any] | None]
    origin: Mapped[str] = mapped_column(String(16))  # batch / on_demand
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None]
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]


class LlmBatch(Base):
    __tablename__ = "llm_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    anthropic_batch_id: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="submitted", index=True)
    request_count: Mapped[int] = mapped_column(Integer)
    meta_json: Mapped[dict[str, Any] | None]
    submitted_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]


class SttTranscript(Base):
    __tablename__ = "stt_transcripts"
    audio_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime]


class Chunk(Base):
    """A searchable piece of a course file (≤ ~400 tokens) with its source locator.
    Mirrored into the FTS5 table `chunks_fts` by triggers (migration 0004)."""

    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("course_files.id", ondelete="CASCADE"), index=True
    )
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    locator: Mapped[str] = mapped_column(String(64))  # "p. 12" / "slide 7" / "§3"
    text: Mapped[str] = mapped_column(Text)
