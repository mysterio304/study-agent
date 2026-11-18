"""Course structure (modules + file metadata). Used by topic discovery and by file
notifications. Network first, then one short DB transaction (no lock held across I/O)."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.canvas.api import CanvasSource
from agent.canvas.sync import content_hash
from agent.core.timeutil import parse_iso_utc
from agent.db.models import CourseFile, CourseModule

PAST_PAPER_RE = re.compile(r"past|exam|final|midterm", re.I)


async def sync_course_structure(
    sessionmaker: async_sessionmaker, source: CanvasSource, course_id: int
) -> list[tuple[int, str, bool, str]]:
    """Fetch and store; returns changed files as (name, is_new)."""
    modules = await source.list_modules(course_id) or []
    files = await source.list_files(course_id)
    async with sessionmaker() as session:
        changed = await store_course_structure(session, course_id, modules, files)
        await session.commit()
    return changed


async def store_course_structure(
    session: AsyncSession,
    course_id: int,
    modules: list[dict[str, Any]],
    files: list[dict[str, Any]] | None,
) -> list[tuple[int, str, bool, str]]:
    for m in modules:
        items = [
            {"title": i.get("title"), "type": i.get("type"), "content_id": i.get("content_id")}
            for i in m.get("items") or []
        ]
        row = await session.get(CourseModule, m["id"]) or CourseModule(id=m["id"])
        row.course_id, row.name, row.position = course_id, m.get("name", ""), m.get("position")
        row.items_json, row.content_hash = items, content_hash(items)
        session.add(row)
    changed: list[tuple[int, str, bool, str]] = []
    seen: set[int] = set()
    for f in files or []:
        seen.add(f["id"])
        change = await _upsert_file(
            session,
            course_id,
            f["id"],
            f.get("display_name", ""),
            f.get("content-type"),
            "files_api",
            updated=f.get("updated_at"),
        )
        if change:
            changed.append(change)
    for m in modules:  # Files tab hidden → files are often still reachable via module items
        for i in m.get("items") or []:
            if i.get("type") == "File" and i.get("content_id") and i["content_id"] not in seen:
                seen.add(i["content_id"])
                change = await _upsert_file(
                    session,
                    course_id,
                    i["content_id"],
                    i.get("title", ""),
                    None,
                    "module_item",
                    module=m.get("name"),
                )
                if change:
                    changed.append(change)
    await session.flush()
    return changed


async def _upsert_file(
    session: AsyncSession,
    course_id: int,
    canvas_id: int,
    name: str,
    ctype: str | None,
    source: str,
    *,
    module: str | None = None,
    updated: str | None = None,
) -> tuple[int, str, bool, str] | None:
    row = await session.scalar(select(CourseFile).where(CourseFile.canvas_file_id == canvas_id))
    is_new = row is None
    row = row or CourseFile(
        canvas_file_id=canvas_id, course_id=course_id, source=source, index_state="pending"
    )
    updated_at = parse_iso_utc(updated)
    was_updated = (
        not is_new
        and updated_at is not None
        and row.canvas_updated_at is not None
        and updated_at > row.canvas_updated_at
    )
    row.display_name, row.content_type = name[:500], ctype or row.content_type
    row.module_name = module or row.module_name
    row.canvas_updated_at = updated_at or row.canvas_updated_at
    row.kind = "past_paper" if PAST_PAPER_RE.search(name) else "lecture"
    if was_updated:
        row.index_state = "pending"  # re-index changed content (M5)
    session.add(row)
    if is_new or was_updated:
        stamp = row.canvas_updated_at.isoformat() if row.canvas_updated_at else ""
        return canvas_id, row.display_name, is_new, stamp
    return None
