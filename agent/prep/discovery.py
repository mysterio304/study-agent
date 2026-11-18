"""Topic discovery (SPEC §6): build a draft topic list per exam from Canvas modules, file names
and the syllabus, then let the student review it.

One Sonnet request per exam, sent through the Batches API (50 % cheaper) with an on-demand
fallback if the batch is slow. Input is compact text (never raw Canvas JSON) within the
`topic_discovery` budget; output is schema-constrained JSON.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.brain.batches import BatchItem, BatchRunner, Purpose
from agent.brain.budgets import truncate_to_tokens
from agent.brain.llm import LlmClient, text_of
from agent.canvas.api import CanvasSource
from agent.canvas.sync import content_hash
from agent.core.clock import Clock
from agent.db.models import Course, CourseFile, CourseModule, Exam, LlmBatch, Topic

log = logging.getLogger(__name__)

PURPOSE = "topic_discovery"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "topics.json"
MAX_TOPICS = 14

DISCOVERY_SYSTEM = (
    "You extract a study topic list for ONE university final exam from course structure. "
    "Group lectures into 6-14 exam topics (not one per lecture). Topic names in English, short. "
    "importance: 3 = core/heavily examined, 2 = normal, 1 = minor. effort_hours: realistic hours for "
    "a Year-3 student to reach 'can solve exam problems' from scratch (1-8). sources: module or file "
    "names the topic comes from. final_weight: the final exam's share of the course grade as a "
    "fraction (0-1) if the syllabus states it, else null."
)

TOPIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "importance": {"type": "integer"},
                    "effort_hours": {"type": "number"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "importance", "effort_hours", "sources"],
                "additionalProperties": False,
            },
        },
        "final_weight": {"type": ["number", "null"]},
    },
    "required": ["topics", "final_weight"],
    "additionalProperties": False,
}

OnReady = Callable[[int], Awaitable[None]]


def strip_html(value: str | None) -> str:
    text = re.sub(r"<(br|p|li|h\d)[^>]*>", "\n", value or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


async def sync_course_structure(
    sessionmaker: async_sessionmaker, source: CanvasSource, course_id: int
) -> None:
    """Fetch module headers/items and file metadata (no downloads; that is M5) and store them.
    Network calls happen before any DB session opens, so no write lock is held across them."""
    modules = await source.list_modules(course_id) or []
    files = await source.list_files(course_id)
    async with sessionmaker() as session:
        await _store_course_structure(session, course_id, modules, files)
        await session.commit()


async def _store_course_structure(
    session: AsyncSession,
    course_id: int,
    modules: list[dict[str, Any]],
    files: list[dict[str, Any]] | None,
) -> None:
    for m in modules:
        items = [
            {"title": i.get("title"), "type": i.get("type"), "content_id": i.get("content_id")}
            for i in m.get("items") or []
        ]
        row = await session.get(CourseModule, m["id"]) or CourseModule(id=m["id"])
        row.course_id, row.name, row.position = course_id, m.get("name", ""), m.get("position")
        row.items_json, row.content_hash = items, content_hash(items)
        session.add(row)
    seen: set[int] = set()
    for f in files or []:
        seen.add(f["id"])
        await _upsert_file(
            session,
            course_id,
            f["id"],
            f.get("display_name", ""),
            f.get("content-type"),
            "files_api",
        )
    for m in modules:  # Files tab hidden → files are often still reachable via module items
        for i in m.get("items") or []:
            if i.get("type") == "File" and i.get("content_id") and i["content_id"] not in seen:
                seen.add(i["content_id"])
                await _upsert_file(
                    session,
                    course_id,
                    i["content_id"],
                    i.get("title", ""),
                    None,
                    "module_item",
                    module=m.get("name"),
                )
    await session.flush()


async def _upsert_file(
    session: AsyncSession,
    course_id: int,
    canvas_id: int,
    name: str,
    ctype: str | None,
    source: str,
    module: str | None = None,
) -> None:
    row = await session.scalar(select(CourseFile).where(CourseFile.canvas_file_id == canvas_id))
    row = row or CourseFile(
        canvas_file_id=canvas_id, course_id=course_id, source=source, index_state="pending"
    )
    row.display_name, row.content_type, row.module_name = name[:500], ctype, module
    row.kind = "past_paper" if re.search(r"past|exam|final|midterm", name, re.I) else "lecture"
    session.add(row)


async def discovery_prompt(session: AsyncSession, exam: Exam) -> str:
    course = await session.get(Course, exam.course_id) if exam.course_id else None
    modules = (
        await session.scalars(
            select(CourseModule)
            .where(CourseModule.course_id == exam.course_id)
            .order_by(CourseModule.position)
        )
    ).all()
    files = (
        await session.scalars(select(CourseFile).where(CourseFile.course_id == exam.course_id))
    ).all()
    mod_text = "\n".join(
        f"- {m.name}: " + "; ".join(str(i.get("title")) for i in (m.items_json or [])[:15])
        for m in modules
    )
    file_text = "\n".join(f"- {f.display_name}" for f in files[:120])
    parts = [
        f"Exam: {exam.title}" + (f" (Canvas course: {course.name})" if course else ""),
        "Syllabus:\n"
        + truncate_to_tokens(strip_html(course.syllabus_html if course else ""), 2_500),
        "Modules:\n" + truncate_to_tokens(mod_text or "(none)", 4_500),
        "Files:\n" + truncate_to_tokens(file_text or "(none)", 2_000),
    ]
    return "\n\n".join(parts)


def parse_topics(raw: str) -> tuple[list[dict[str, Any]], float | None]:
    data = json.loads(raw)
    topics = []
    for t in data.get("topics", [])[:MAX_TOPICS]:
        name = str(t.get("name", "")).strip()[:120]
        if not name:
            continue
        topics.append(
            {
                "name": name,
                "importance": max(1, min(3, int(t.get("importance", 2)))),
                "effort_hours": max(0.5, min(12.0, float(t.get("effort_hours", 3)))),
                "sources": [str(x)[:120] for x in t.get("sources", [])][:6],
            }
        )
    weight = data.get("final_weight")
    weight = float(weight) if isinstance(weight, int | float) and 0 < weight <= 1 else None
    return topics, weight


async def save_draft_topics(
    session: AsyncSession, exam_id: int, topics: list[dict[str, Any]], final_weight: float | None
) -> int:
    existing = {
        t.name.lower()
        for t in (await session.scalars(select(Topic).where(Topic.exam_id == exam_id))).all()
    }
    added = 0
    for t in topics:
        if t["name"].lower() in existing:
            continue
        session.add(
            Topic(
                exam_id=exam_id,
                name=t["name"],
                importance=t["importance"],
                effort_hours=t["effort_hours"],
                source_refs_json=t["sources"],
                level=0,
                target_level=3 if t["importance"] == 3 else 2,
                status="draft",
            )
        )
        added += 1
    exam = await session.get(Exam, exam_id)
    if exam and final_weight is not None and exam.weight_source == "unknown":
        exam.final_weight, exam.weight_source = final_weight, "syllabus"
    await session.flush()
    return added


class TopicDiscovery:
    """Orchestrates discovery for exams that have no topics yet."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        source: CanvasSource,
        llm: LlmClient | None,
        batches: BatchRunner | None,
        clock: Clock,
        on_ready: OnReady,
    ) -> None:
        self._sm, self._source, self._llm, self._batches, self._clock = (
            sessionmaker,
            source,
            llm,
            batches,
            clock,
        )
        self._on_ready = on_ready
        if batches is not None:
            batches.register(PURPOSE, Purpose(self._on_batch_results, self._on_batch_timeout))

    async def in_progress(self) -> bool:
        async with self._sm() as s:
            row = await s.scalar(
                select(LlmBatch.id).where(
                    LlmBatch.purpose == PURPOSE, LlmBatch.status == "submitted"
                )
            )
        return row is not None

    async def start(self) -> list[int]:
        """Kick off discovery; returns exam ids being processed."""
        async with self._sm() as s:
            in_flight = {
                exam_id
                for b in (
                    await s.scalars(
                        select(LlmBatch).where(
                            LlmBatch.purpose == PURPOSE, LlmBatch.status == "submitted"
                        )
                    )
                ).all()
                for exam_id in (b.meta_json or {}).get("exam_ids", [])
            }
            exams = [
                e
                for e in (await s.scalars(select(Exam).order_by(Exam.starts_at))).all()
                if e.id not in in_flight
                and not (await s.scalars(select(Topic.id).where(Topic.exam_id == e.id))).first()
            ]
        for e in exams:
            if e.course_id:
                await sync_course_structure(self._sm, self._source, e.course_id)
        async with self._sm() as s:
            prompts = {e.id: await discovery_prompt(s, e) for e in exams}
        if not exams:
            return []
        if self._llm is None:
            await self._from_fixture([e.id for e in exams])
        elif self._batches is not None:
            items = [
                BatchItem(
                    f"exam-{eid}",
                    PURPOSE,
                    DISCOVERY_SYSTEM,
                    [{"role": "user", "content": p}],
                    TOPIC_SCHEMA,
                )
                for eid, p in prompts.items()
            ]
            await self._batches.submit(PURPOSE, items, {"exam_ids": list(prompts)})
        else:
            await self._on_demand(list(prompts))
        return list(prompts)

    async def _on_batch_results(self, row: LlmBatch, results: dict[str, Any | None]) -> None:
        failed = []
        for custom_id, msg in results.items():
            exam_id = int(custom_id.removeprefix("exam-"))
            if msg is None:
                failed.append(exam_id)
                continue
            await self._store(exam_id, text_of(msg))
        if failed:
            await self._on_demand(failed)

    async def _on_batch_timeout(self, row: LlmBatch) -> None:
        await self._on_demand(list((row.meta_json or {}).get("exam_ids", [])))

    async def _on_demand(self, exam_ids: list[int]) -> None:
        assert self._llm is not None
        for exam_id in exam_ids:
            async with self._sm() as s:
                exam = await s.get(Exam, exam_id)
                prompt = await discovery_prompt(s, exam)
            res = await self._llm.call(
                PURPOSE,
                system=DISCOVERY_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                json_schema=TOPIC_SCHEMA,
            )
            await self._store(exam_id, text_of(res.message))

    async def _store(self, exam_id: int, raw: str) -> None:
        try:
            topics, weight = parse_topics(raw)
        except (ValueError, TypeError) as exc:
            log.error("Topic discovery for exam %s returned invalid JSON: %s", exam_id, exc)
            return
        async with self._sm() as s:
            await save_draft_topics(s, exam_id, topics, weight)
            await s.commit()
        await self._on_ready(exam_id)

    async def _from_fixture(self, exam_ids: list[int]) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for exam_id in exam_ids:
            async with self._sm() as s:
                exam = await s.get(Exam, exam_id)
                entry = data.get(exam.title, {"topics": [], "final_weight": None})
            await self._store(exam_id, json.dumps(entry))
